$ErrorActionPreference = 'Stop'
$scenes = Get-Content -Raw -Encoding UTF8 (Join-Path $PSScriptRoot 'scenes.json') | ConvertFrom-Json
$voice = New-Object -ComObject SAPI.SpVoice
$token = New-Object -ComObject SAPI.SpObjectToken
$token.SetId('HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens\MSTTS_V110_ruRU_IrinaM')
$voice.Voice = $token
$voice.Rate = 1
for ($i=0; $i -lt $scenes.Count; $i++) {
    $stream = New-Object -ComObject SAPI.SpFileStream
    $stream.Open((Join-Path $PSScriptRoot ('voice-{0:00}.wav' -f $i)),3)
    $voice.AudioOutputStream = $stream
    $null = $voice.Speak($scenes[$i].narration)
    $stream.Close()
    Write-Output "Narrated $i"
}
