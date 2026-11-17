param(
    [string]$Url = "http://127.0.0.1:8080",
    [string]$Output = "results/long-stream.log"
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Net.Http
$projectRoot = Split-Path -Parent $PSScriptRoot
$outputPath = Join-Path $projectRoot $Output
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $outputPath) | Out-Null
$requestId = [guid]::NewGuid().ToString()
$payload = @{
    prompt = "long-rollout-stream"
    session_id = "rollout-stream"
    history = @(@{ role = "user"; content = "cross the beginning of a rollout" })
    prefix = "rollout-prefix"
    max_new_tokens = 32
} | ConvertTo-Json -Depth 5 -Compress
$client = [System.Net.Http.HttpClient]::new()
$reader = $null
$response = $null
$request = [System.Net.Http.HttpRequestMessage]::new(
    [System.Net.Http.HttpMethod]::Post,
    "$($Url.TrimEnd('/'))/generate"
)
$request.Headers.Add("X-Request-ID", $requestId)
Add-Content -LiteralPath $outputPath -Value "$(Get-Date -Format o) request_id=$requestId" -Encoding utf8
$request.Content = [System.Net.Http.StringContent]::new(
    $payload,
    [System.Text.Encoding]::UTF8,
    "application/json"
)

try {
    $response = $client.SendAsync(
        $request,
        [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead
    ).GetAwaiter().GetResult()
    if (-not $response.IsSuccessStatusCode) {
        $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        throw "Stream request failed with HTTP $([int]$response.StatusCode): $body"
    }

    $reader = [System.IO.StreamReader]::new(
        $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
    )
    while (-not $reader.EndOfStream) {
        $line = $reader.ReadLine()
        if ($line) {
            $record = "$(Get-Date -Format o) $line"
            Add-Content -LiteralPath $outputPath -Value $record -Encoding utf8
            Write-Output $record
        }
    }
    Write-Output "request_id=$requestId"
}
finally {
    if ($reader) { $reader.Dispose() }
    if ($response) { $response.Dispose() }
    $request.Dispose()
    $client.Dispose()
}
