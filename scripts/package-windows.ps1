# Builds a distributable Windows plugin folder, optionally signed.
#
#   .\scripts\package-windows.ps1
#   .\scripts\package-windows.ps1 -Sign
#
# OBS expects the layout
#   %ProgramData%\obs-studio\plugins\<name>\bin\64bit\<name>.dll
# so that is what is produced, ready to copy in whole.
#
# Pure ASCII, deliberately: this has to run under Windows PowerShell 5.1,
# which mangles non-ASCII in a script file.
param([switch]$Sign)

$ErrorActionPreference = "Stop"

$Name      = "studio-mode-meters"
$PluginDir = "target\$Name"
$Dll       = "$PluginDir\bin\64bit\$Name.dll"

$VersionLine = Select-String -Path Cargo.toml -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
$Version = $VersionLine.Matches.Groups[1].Value

# Keep the builder's home directory out of the shipped binary. rustc embeds
# absolute source paths in panic metadata, so an unconfigured build ships
# `C:\Users\<name>\.rustup\...` to everyone who downloads it. See
# package-macos.sh for the longer note; `[profile.release] trim-paths` would
# be the natural home for this and is unstable in Cargo 1.95.0.
$env:RUSTFLAGS = "$($env:RUSTFLAGS) --remap-path-prefix=$($env:USERPROFILE)=~ --remap-path-prefix=$($PWD.Path)=."

Write-Host "==> Building $Name $Version (release)"
cargo build --release
if ($LASTEXITCODE -ne 0) { throw "cargo build failed" }

if (Test-Path $PluginDir) { Remove-Item -Recurse -Force $PluginDir }
New-Item -ItemType Directory -Path "$PluginDir\bin\64bit" -Force | Out-Null
Copy-Item "target\release\obs_studio_mode_meters.dll" $Dll

if ($Sign) {
    # Azure Trusted Signing, same identity FrameSW uses. Authenticates
    # through the caller's `az login` session (AzureCliCredential), not
    # environment variables.
    #
    # NEVER recreate the certificate profile: SmartScreen reputation
    # accrues to the signing identity, and a new profile starts from zero.
    Write-Host "==> Signing"
    $dlib = "$env:LOCALAPPDATA\FrameSW\trusted-signing\Azure.CodeSigning.Dlib.dll"
    if (-not (Test-Path $dlib)) { throw "Trusted Signing dlib not found at $dlib" }
    $meta = "$env:TEMP\sms-metadata.json"
    @{
        Endpoint               = "https://eus.codesigning.azure.net/"
        CodeSigningAccountName = "hoversights"
        CertificateProfileName = "hoversights-public-trust"
    } | ConvertTo-Json | Set-Content -Path $meta -Encoding ascii

    & signtool sign /v /debug /fd SHA256 /tr "http://timestamp.acs.microsoft.com" `
        /td SHA256 /dlib $dlib /dmdf $meta $Dll
    if ($LASTEXITCODE -ne 0) { throw "signing failed" }
    & signtool verify /pa /v $Dll
    if ($LASTEXITCODE -ne 0) { throw "signature verification failed" }
}

$Zip = "target\$Name-$Version-windows.zip"
if (Test-Path $Zip) { Remove-Item $Zip }
Compress-Archive -Path $PluginDir -DestinationPath $Zip
Write-Host ""
Write-Host "Built: $Zip"
Write-Host "Install by copying the '$Name' folder into:"
Write-Host "  %ProgramData%\obs-studio\plugins\"
