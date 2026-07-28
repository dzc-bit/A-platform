[CmdletBinding()]
param(
    [ValidateSet("up", "down", "ps", "health", "smoke", "config")]
    [string]$Action = "up",
    [switch]$WithDify,
    [int]$DifyPort = 0,
    [int]$TimeoutSeconds = 240,
    [string]$DifyComposeDir = "",
    [string]$DifyProjectName = ""
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$AppCompose = Join-Path $Root "compose.yaml"
$AppEnv = Join-Path $Root ".env"
$AppEnvExample = Join-Path $Root ".env.example"
# The repository copy is the safe default. An operator can point at an
# already-running official Dify checkout without creating a second stack.
$DifyDirInput = if (-not [string]::IsNullOrWhiteSpace($DifyComposeDir)) {
    $DifyComposeDir
}
elseif (-not [string]::IsNullOrWhiteSpace($env:DIFY_COMPOSE_DIR)) {
    $env:DIFY_COMPOSE_DIR
}
else {
    Join-Path $Root ".runtime\dify\docker"
}
$DifyDir = if ([IO.Path]::IsPathRooted($DifyDirInput)) {
    [IO.Path]::GetFullPath($DifyDirInput)
}
else {
    [IO.Path]::GetFullPath((Join-Path $Root $DifyDirInput))
}
$DifyCompose = Join-Path $DifyDir "docker-compose.yaml"
$DifyEnv = Join-Path $DifyDir ".env"
$AppProject = if ($env:COMPOSE_PROJECT_NAME) { $env:COMPOSE_PROJECT_NAME } else { "neusoft-business-ai" }
$DifyProject = if (-not [string]::IsNullOrWhiteSpace($DifyProjectName)) {
    $DifyProjectName
}
elseif (-not [string]::IsNullOrWhiteSpace($env:DIFY_COMPOSE_PROJECT_NAME)) {
    $env:DIFY_COMPOSE_PROJECT_NAME
}
else {
    "neusoft-dify"
}

function Fail([string]$Message) {
    throw $Message
}

function Assert-DockerCli {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Fail "Docker CLI was not found. Install Docker Desktop/Engine, then rerun this command."
    }
}

function Assert-DockerDaemon {
    Assert-DockerCli
    & docker info --format "{{.ServerVersion}}" *> $null
    if ($LASTEXITCODE -ne 0) {
        Fail "Docker Engine is unavailable. Start Docker Desktop (Linux engine), wait for it to become ready, then rerun the command."
    }
}

function Invoke-Compose {
    param(
        [Parameter(Mandatory = $true)][string]$Directory,
        [Parameter(Mandatory = $true)][string]$Project,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    Push-Location $Directory
    try {
        & docker compose --project-name $Project @Arguments
        if ($LASTEXITCODE -ne 0) {
            Fail "docker compose failed for project '$Project' (exit code $LASTEXITCODE)."
        }
    }
    finally {
        Pop-Location
    }
}

function Get-EnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }
    $line = Get-Content -LiteralPath $Path -Encoding utf8 |
        Where-Object { $_ -match "^\s*$([regex]::Escape($Name))\s*=" } |
        Select-Object -First 1
    if (-not $line) {
        return $null
    }
    return (($line -replace "^\s*$([regex]::Escape($Name))\s*=\s*", "") -replace "\s+#.*$", "").Trim()
}

function Assert-DifyFiles {
    if (-not (Test-Path -LiteralPath $DifyCompose)) {
        Fail "Official Dify compose file is missing: $DifyCompose"
    }
    if (-not (Test-Path -LiteralPath $DifyEnv)) {
        Fail "Dify environment file is missing: $DifyEnv. Create it without committing secrets: Copy-Item .runtime\dify\docker\.env.example .runtime\dify\docker\.env"
    }
}

function Get-DifyHostPort {
    $configured = Get-EnvValue -Path $DifyEnv -Name "EXPOSE_NGINX_PORT"
    if ($configured -and $configured -match "^(?:[^:]+:)?(?<host>\d+):\d+$") {
        return [int]$Matches.host
    }
    if ($configured -and $configured -match ":(?<port>\d+)$") {
        return [int]$Matches.port
    }
    if ($configured -and $configured -match "^(?<port>\d+)$") {
        return [int]$Matches.port
    }
    return 8081
}

function Get-DifyProfiles {
    $profiles = @()
    $dbType = Get-EnvValue -Path $DifyEnv -Name "DB_TYPE"
    $vectorStore = Get-EnvValue -Path $DifyEnv -Name "VECTOR_STORE"
    if ($dbType) { $profiles += $dbType } else { $profiles += "postgresql" }
    if ($vectorStore) { $profiles += $vectorStore } else { $profiles += "weaviate" }
    $configuredProfiles = Get-EnvValue -Path $DifyEnv -Name "COMPOSE_PROFILES"
    # The official default includes collaboration. Respect an explicit custom
    # profile list instead of silently enabling extra services.
    if (-not $configuredProfiles -or $configuredProfiles -match "(^|,)\s*collaboration\s*(,|$)") {
        $profiles += "collaboration"
    }
    return @($profiles | Select-Object -Unique)
}

function Get-AppEnvFile {
    if (Test-Path -LiteralPath $AppEnv) { return ".env" }
    if (Test-Path -LiteralPath $AppEnvExample) { return ".env.example" }
    Fail "Neither .env nor .env.example exists at the project root."
}

function Invoke-DifyCompose {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    Assert-DifyFiles
    Invoke-Compose -Directory $DifyDir -Project $DifyProject -Arguments $Arguments
}

function Invoke-AppCompose {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $envFile = Get-AppEnvFile
    $composeArgs = @("--file", $AppCompose, "--env-file", (Join-Path $Root $envFile)) + $Arguments
    Invoke-Compose -Directory $Root -Project $AppProject -Arguments $composeArgs
}

function Sync-DifyNginxUpstreams {
    # Docker Desktop can restart the API container with a new address while
    # the long-running official Nginx container still holds the old DNS result.
    $difyArgs = @("--env-file", ".env")
    foreach ($profile in (Get-DifyProfiles)) { $difyArgs += @("--profile", $profile) }
    $difyArgs += @("exec", "-T", "nginx", "nginx", "-s", "reload")
    Invoke-DifyCompose -Arguments $difyArgs
}

function Invoke-HttpCheck {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Uri,
        [int]$Timeout = $TimeoutSeconds
    )

    $deadline = (Get-Date).AddSeconds($Timeout)
    $lastError = "request did not return a successful response"
    do {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 8
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400) {
                return [pscustomobject]@{ name = $Name; status = "ok"; uri = $Uri; code = $response.StatusCode }
            }
            $lastError = "HTTP $($response.StatusCode)"
        }
        catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)

    Fail "$Name health check failed at ${Uri}: $lastError"
}

function Invoke-Health {
    $checks = @()
    $checks += Invoke-HttpCheck -Name "backend" -Uri "http://127.0.0.1:8000/api/v1/health"
    $checks += Invoke-HttpCheck -Name "frontend" -Uri "http://127.0.0.1:5173/"
    if ($WithDify) {
        Assert-DifyFiles
        $port = if ($DifyPort -gt 0) { $DifyPort } else { Get-DifyHostPort }
        $checks += Invoke-HttpCheck -Name "dify-web" -Uri "http://127.0.0.1:$port/"
        $checks += Invoke-HttpCheck -Name "dify-api" -Uri "http://127.0.0.1:$port/console/api/setup"
    }
    [pscustomobject]@{
        checked_at = (Get-Date).ToUniversalTime().ToString("o")
        app_project = $AppProject
        dify_project = if ($WithDify) { $DifyProject } else { $null }
        checks = $checks
        note = "HTTP reachability only; Dify workflow import, model credentials, dataset binding, and media-provider responses require manual acceptance."
    } | ConvertTo-Json -Depth 5
}

function Invoke-DemoSmoke {
    if ($WithDify) {
        # Keep the Dify reachability check separate from the business request;
        # a Dify app key/workflow still has to be configured by an operator.
        Invoke-Health | Out-Host
    }
    $baseUri = "http://127.0.0.1:5173/api/v1"
    try {
        if (-not $env:DEMO_PASSWORD) { Fail "DEMO_PASSWORD is required for demo smoke login." }
        $loginBody = @{ email = "enterprise@neusoft.local"; password = $env:DEMO_PASSWORD } | ConvertTo-Json
        $login = Invoke-RestMethod -Method Post -Uri "$baseUri/auth/login" -ContentType "application/json" -Body $loginBody -TimeoutSec 15
        if (-not $login.access_token) { Fail "Demo smoke login returned no access token." }
        $headers = @{ Authorization = "Bearer $($login.access_token)" }
        $chatBody = @{ message = "开票申请需要准备什么材料？"; mode = "knowledge" } | ConvertTo-Json
        $chat = Invoke-RestMethod -Method Post -Uri "$baseUri/assistant/chat" -Headers $headers -ContentType "application/json" -Body $chatBody -TimeoutSec 45
        if (-not $chat.answer -or -not $chat.citations) { Fail "Demo smoke chat returned no answer or citations." }
        [pscustomobject]@{
            status = "ok"
            route = "frontend proxy -> FastAPI -> local RAG/agent"
            answer_present = $true
            citation_count = @($chat.citations).Count
            used_fallback = [bool]$chat.used_fallback
            note = "This validates the seeded classroom path; a fallback answer is reported honestly and is not a Dify success claim."
        } | ConvertTo-Json -Depth 5
    }
    catch {
        Fail "Demo smoke failed: $($_.Exception.Message)"
    }
}

Assert-DockerCli

switch ($Action) {
    "config" {
        $appConfigArgs = @("config", "-q")
        Invoke-AppCompose -Arguments $appConfigArgs
        if ($WithDify) {
            Assert-DifyFiles
            $difyArgs = @("--env-file", ".env")
            foreach ($profile in (Get-DifyProfiles)) { $difyArgs += @("--profile", $profile) }
            $difyArgs += @("config", "-q")
            Invoke-DifyCompose -Arguments $difyArgs
        }
        Write-Output "Compose configuration is valid."
    }
    "up" {
        Assert-DockerDaemon
        $previousUrl = [Environment]::GetEnvironmentVariable("DIFY_API_URL", "Process")
        $previousPort = [Environment]::GetEnvironmentVariable("EXPOSE_NGINX_PORT", "Process")
        $previousTrigger = [Environment]::GetEnvironmentVariable("TRIGGER_URL", "Process")
        try {
            if ($WithDify) {
                Assert-DifyFiles
                $port = if ($DifyPort -gt 0) { $DifyPort } else { Get-DifyHostPort }
                $env:EXPOSE_NGINX_PORT = "127.0.0.1:$port"
                $env:TRIGGER_URL = "http://localhost:$port"
                $difyArgs = @("--env-file", ".env")
                foreach ($profile in (Get-DifyProfiles)) { $difyArgs += @("--profile", $profile) }
                $difyArgs += @("up", "-d", "--wait", "--wait-timeout", $TimeoutSeconds.ToString())
                Invoke-DifyCompose -Arguments $difyArgs
                Sync-DifyNginxUpstreams
                if (-not $env:DIFY_API_URL) {
                    $env:DIFY_API_URL = "http://host.docker.internal:$port"
                }
            }
            $appArgs = @("up", "-d", "--build", "--wait", "--wait-timeout", $TimeoutSeconds.ToString())
            Invoke-AppCompose -Arguments $appArgs
        }
        finally {
            if ($null -eq $previousUrl) { Remove-Item Env:DIFY_API_URL -ErrorAction SilentlyContinue } else { $env:DIFY_API_URL = $previousUrl }
            if ($null -eq $previousPort) { Remove-Item Env:EXPOSE_NGINX_PORT -ErrorAction SilentlyContinue } else { $env:EXPOSE_NGINX_PORT = $previousPort }
            if ($null -eq $previousTrigger) { Remove-Item Env:TRIGGER_URL -ErrorAction SilentlyContinue } else { $env:TRIGGER_URL = $previousTrigger }
        }
        Invoke-Health
    }
    "down" {
        Assert-DockerDaemon
        Invoke-AppCompose -Arguments @("down")
        if ($WithDify) {
            Assert-DifyFiles
            $difyArgs = @("--env-file", ".env")
            foreach ($profile in (Get-DifyProfiles)) { $difyArgs += @("--profile", $profile) }
            $difyArgs += @("down")
            Invoke-DifyCompose -Arguments $difyArgs
        }
    }
    "ps" {
        Assert-DockerDaemon
        Invoke-AppCompose -Arguments @("ps")
        if ($WithDify) {
            Assert-DifyFiles
            Invoke-DifyCompose -Arguments @("--env-file", ".env", "ps")
        }
    }
    "health" {
        Invoke-Health
    }
    "smoke" {
        Invoke-DemoSmoke
    }
}
