<#
  Render a UTF-8 text bill into a PNG image, used to create test "bill screenshots".

  Usage:
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\make_bill_image.ps1 `
        -TextFile samples\bill_sample.txt -OutPath samples\bill_sample.png

  IMPORTANT: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 files using
  the system ANSI code page unless a BOM is present, so non-ASCII characters can break
  parsing. All Chinese content comes from -TextFile (UTF-8), never from this script.

  Defaults are tuned to be OCR friendly: DengXian font, larger size, generous line
  spacing, so the Windows OCR engine returns one complete record per line.
#>
param(
    [Parameter(Mandatory = $true)][string]$TextFile,
    [Parameter(Mandatory = $true)][string]$OutPath,
    [int]$Width = 900,
    [int]$FontSize = 20,
    [double]$LineSpacing = 3.6,
    [int]$Padding = 28,
    [string]$FontName = "DengXian"
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

$lines = @(Get-Content -LiteralPath $TextFile -Encoding UTF8)
$lineHeight = [int]($FontSize * $LineSpacing)
$height = 70 + ($lines.Count * $lineHeight) + 40

$bitmap = New-Object -TypeName System.Drawing.Bitmap -ArgumentList $Width, $height
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
$graphics.Clear([System.Drawing.Color]::White)

$font = New-Object -TypeName System.Drawing.Font -ArgumentList $FontName, $FontSize
$smallFont = New-Object -TypeName System.Drawing.Font -ArgumentList $FontName, ($FontSize - 3)
$linePen = New-Object -TypeName System.Drawing.Pen -ArgumentList ([System.Drawing.Color]::FromArgb(210, 210, 210)), 1

$y = 28
for ($i = 0; $i -lt $lines.Count; $i++) {
    $text = $lines[$i]
    if ($text.Trim() -match '^-{3,}$') {
        $graphics.DrawLine($linePen, $Padding, ($y + $lineHeight / 2), ($Width - $Padding), ($y + $lineHeight / 2))
    }
    elseif ($i -eq 0) {
        $graphics.DrawString($text, $font, [System.Drawing.Brushes]::Black, 28, $y)
    }
    else {
        $graphics.DrawString($text, $font, [System.Drawing.Brushes]::Black, 28, $y)
    }
    $y += $lineHeight
}

$graphics.DrawString("(sample image generated for demo only)", $smallFont,
    [System.Drawing.Brushes]::Gray, 28, ($height - 34))

$graphics.Dispose()
$bitmap.Save($OutPath, [System.Drawing.Imaging.ImageFormat]::Png)
$bitmap.Dispose()
Write-Output ("saved: " + $OutPath + " (" + $Width + "x" + $height + ", " + $lines.Count + " lines)")
