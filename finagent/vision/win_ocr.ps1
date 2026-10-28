<#
  Windows built-in OCR engine wrapper (Windows.Media.Ocr).

  Why: Windows 10/11 ships an offline OCR engine, so bill screenshots can be read
  without installing any third party Python package, even without network access.

  Note: WinRT type projection is only available in Windows PowerShell 5.1
  (powershell.exe). PowerShell 7 (pwsh) removed WinRT support, so callers must
  invoke powershell.exe, not pwsh.

  Recognized lines are written to -OutFile as UTF-8, which avoids mojibake caused by
  console code pages. Keep this file ASCII-only (see make_bill_image.ps1 for why).
#>
param(
    [Parameter(Mandatory = $true)][string]$ImagePath,
    [Parameter(Mandatory = $true)][string]$OutFile,
    [string]$Language = ""
)

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Runtime.WindowsRuntime

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and
    $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]

function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}

[void][Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
[void][Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
[void][Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType = WindowsRuntime]
[void][Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]

if (-not (Test-Path -LiteralPath $ImagePath)) {
    Write-Error "image not found: $ImagePath"
    exit 3
}

$fullImage = (Resolve-Path -LiteralPath $ImagePath).Path
$workingImage = $fullImage
$tempImage = $null

# Windows.Media.Ocr rejects very large bitmaps. Downscale while preserving aspect ratio.
Add-Type -AssemblyName System.Drawing
$sourceImage = [System.Drawing.Image]::FromFile($fullImage)
try {
    $maxDimension = 4096
    if ($sourceImage.Width -gt $maxDimension -or $sourceImage.Height -gt $maxDimension) {
        $ratio = [Math]::Min(
            $maxDimension / [double]$sourceImage.Width,
            $maxDimension / [double]$sourceImage.Height
        )
        $newWidth = [Math]::Max(1, [int][Math]::Floor($sourceImage.Width * $ratio))
        $newHeight = [Math]::Max(1, [int][Math]::Floor($sourceImage.Height * $ratio))
        $scaled = New-Object System.Drawing.Bitmap($newWidth, $newHeight)
        $graphics = [System.Drawing.Graphics]::FromImage($scaled)
        try {
            $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
            $graphics.DrawImage($sourceImage, 0, 0, $newWidth, $newHeight)
        } finally {
            $graphics.Dispose()
        }
        $tempImage = Join-Path ([System.IO.Path]::GetTempPath()) ("finagent_ocr_" + [guid]::NewGuid().ToString("N") + ".png")
        $scaled.Save($tempImage, [System.Drawing.Imaging.ImageFormat]::Png)
        $scaled.Dispose()
        $workingImage = $tempImage
    }
} finally {
    $sourceImage.Dispose()
}

$engine = $null
if ($Language -ne "") {
    try {
        $lang = New-Object Windows.Globalization.Language($Language)
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
    } catch {
        $engine = $null
    }
}
if (-not $engine) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
}
if (-not $engine) {
    foreach ($candidate in [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages) {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($candidate)
        if ($engine) { break }
    }
}
if (-not $engine) {
    Write-Error "no OCR recognizer available on this machine"
    exit 4
}

$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($workingImage)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$stream.Dispose()
if ($tempImage -and (Test-Path -LiteralPath $tempImage)) {
    Remove-Item -LiteralPath $tempImage -Force -ErrorAction SilentlyContinue
}

$lines = @()
foreach ($line in $result.Lines) { $lines += $line.Text }

$utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllLines($OutFile, [string[]]$lines, $utf8)

Write-Output ("ok lang=" + $engine.RecognizerLanguage.LanguageTag + " lines=" + $lines.Count)
