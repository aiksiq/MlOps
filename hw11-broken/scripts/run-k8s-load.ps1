param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z0-9][a-z0-9-]{0,30}$')]
    [string]$RunId,
    [Parameter(Mandatory = $true)]
    [ValidateSet("balanced", "hot")]
    [string]$Scenario,
    [Parameter(Mandatory = $true)]
    [ValidateSet("normal", "affinity")]
    [string]$Policy,
    [int]$DurationSeconds = 120,
    [double]$TotalRps = 8
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$releaseName = "hw11"
$namespace = "hw11"
$jobName = "hw11-hw11-simulator-loadgen-$RunId"
$affinity = $Policy -eq "affinity"
$totalRpsJson = $TotalRps.ToString("R", [Globalization.CultureInfo]::InvariantCulture)

Push-Location $projectRoot
try {
    helm upgrade --install $releaseName .\chart `
        --namespace $namespace `
        --reuse-values `
        --set loadgen.enabled=true `
        --set-string "loadgen.runId=$RunId" `
        --set-string "loadgen.scenario=$Scenario" `
        --set loadgen.affinity=$affinity `
        --set loadgen.durationSeconds=$DurationSeconds `
        --set-json "loadgen.totalRps=$totalRpsJson" `
        --wait --timeout 5m
    if ($LASTEXITCODE -ne 0) { throw "Helm update failed." }

    kubectl wait --for=condition=complete "job/$jobName" -n $namespace --timeout "$($DurationSeconds + 660)s"
    if ($LASTEXITCODE -ne 0) { throw "Load-generation Job failed; inspect its Pods and logs before retrying." }

    $job = kubectl get job $jobName -n $namespace -o json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw "Could not read the completed Job." }

    $resultsDir = Join-Path $projectRoot "results"
    New-Item -ItemType Directory -Force -Path $resultsDir | Out-Null
    $combined = Join-Path $resultsDir "$RunId.csv"
    $first = $true
    $pods = kubectl get pods -n $namespace -l "batch.kubernetes.io/job-name=$jobName" -o json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw "Could not list load-generation Pods." }
    foreach ($pod in $pods.items) {
        $localRelative = Join-Path "results" "$RunId-$($pod.metadata.name).csv"
        $logs = kubectl logs -n $namespace $pod.metadata.name -c loadgen
        if ($LASTEXITCODE -ne 0) { throw "Could not read results from Pod $($pod.metadata.name)." }
        $lines = @($logs -split "`r?`n")
        $begin = [Array]::IndexOf($lines, "HW11_CSV_BEGIN")
        $end = [Array]::IndexOf($lines, "HW11_CSV_END")
        if ($begin -lt 0 -or $end -le $begin) {
            throw "Pod $($pod.metadata.name) did not emit a complete CSV result."
        }
        $csv = ($lines[($begin + 1)..($end - 1)] -join "`n") + "`n"
        Set-Content -LiteralPath $localRelative -Value $csv -Encoding utf8
        $local = Join-Path $projectRoot $localRelative
        $rows = Import-Csv $local
        if ($first) {
            $rows | Export-Csv -Path $combined -NoTypeInformation -Encoding utf8
            $first = $false
        } else {
            $rows | Export-Csv -Path $combined -NoTypeInformation -Encoding utf8 -Append
        }
    }

    $python = Join-Path $projectRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) { $python = (Get-Command python -ErrorAction Stop).Source }
    & $python (Join-Path $projectRoot "aggregate_results.py") `
        --input $combined `
        --run-id $RunId `
        --scenario $Scenario `
        --policy $Policy `
        --duration $DurationSeconds `
        --total-rps $TotalRps `
        --output (Join-Path $resultsDir "$RunId.csv")
    if ($LASTEXITCODE -ne 0) { throw "Could not aggregate request results." }
    Write-Output "Saved $combined and its summary. Copy all HPA/Pod/EndpointSlice evidence before the next run."
}
finally {
    Pop-Location
}
