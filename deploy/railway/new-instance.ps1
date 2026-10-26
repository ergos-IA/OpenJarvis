<#
.SYNOPSIS
  Crea una instancia nueva de OpenJarvis en Railway para una persona.

.DESCRIPTION
  Crea un servicio "jarvis-<nombre>" en el proyecto de Railway enlazado,
  con su propio volumen (/data), su propia clave de acceso y un dominio
  público. Al final imprime el enlace para vincular WhatsApp.

  Requisitos (una sola vez):
    npm i -g @railway/cli      # o usa npx @railway/cli
    railway login
    railway link               # elige el proyecto donde viven las instancias
  y en el proyecto, variables compartidas (Project Settings > Shared Variables):
    OPENAI_API_KEY, ANTHROPIC_API_KEY

.EXAMPLE
  ./deploy/railway/new-instance.ps1 -Name maria
  ./deploy/railway/new-instance.ps1 -Name pepe -Model claude-sonnet-4-6 -AnthropicKey sk-ant-...
#>
param(
  [Parameter(Mandatory = $true)][string]$Name,
  [string]$Model = "gpt-4o-mini",
  [string]$Repo = "ergos-IA/OpenJarvis",
  [string]$Branch = "main",
  [string]$AssistantName = "Jarvis",
  [ValidateSet("self", "own")][string]$WhatsAppMode = "self",
  [string]$AllowedNumbers = "",
  # Vacías = usar las variables compartidas del proyecto.
  [string]$OpenAIKey = "",
  [string]$AnthropicKey = "",
  # Sube el código de esta carpeta (railway up) en vez de enlazar el repo de
  # GitHub. Útil si Railway no tiene acceso a GitHub; sin auto-deploy al hacer push.
  [switch]$Local
)

$ErrorActionPreference = "Stop"

# Always an array (a one-element if/else result unrolls to a string). Prefer
# railway.cmd over the npm railway.ps1 shim.
$railway = @(if (Get-Command railway.cmd -ErrorAction SilentlyContinue) { @("railway.cmd") } elseif (Get-Command railway -ErrorAction SilentlyContinue) { @("railway") } else { @("npx.cmd", "-y", "@railway/cli@latest") })
function Invoke-Railway {
  # Flatten: accepts loose words or one array of words (no splatting, which
  # PowerShell 5.1 mangles for "--flag" style arguments).
  $words = @($args | ForEach-Object { $_ })
  $exe = $railway[0]
  $rest = @($railway | Select-Object -Skip 1) + $words
  & $exe $rest
  if ($LASTEXITCODE -ne 0) { throw "railway $($words -join ' ') falló (código $LASTEXITCODE)" }
}

$slug = ($Name.ToLower() -replace "[^a-z0-9-]", "-").Trim("-")
$service = "jarvis-$slug"

$bytes = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$apiKey = "oj_sk_" + ([Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_"))

$openai = if ($OpenAIKey) { $OpenAIKey } else { '${{shared.OPENAI_API_KEY}}' }
$anthropic = if ($AnthropicKey) { $AnthropicKey } else { '${{shared.ANTHROPIC_API_KEY}}' }

$vars = @(
  "PORT=8000",
  # railway.toml is not always honoured (e.g. on `railway up`); this is.
  "RAILWAY_DOCKERFILE_PATH=deploy/railway/Dockerfile",
  "OPENJARVIS_API_KEY=$apiKey",
  "JARVIS_MODEL=$Model",
  "JARVIS_ASSISTANT_NAME=$AssistantName",
  "WHATSAPP_MODE=$WhatsAppMode",
  "OPENAI_API_KEY=$openai",
  "ANTHROPIC_API_KEY=$anthropic"
)
if ($AllowedNumbers) { $vars += "WHATSAPP_ALLOWED_NUMBERS=$AllowedNumbers" }

if ($Local) {
  Write-Host "==> Creando servicio $service (código local)..."
  $addArgs = @("add", "--service", $service)
} else {
  Write-Host "==> Creando servicio $service desde $Repo ($Branch)..."
  $addArgs = @("add", "--service", $service, "--repo", $Repo, "--branch", $Branch)
}
foreach ($v in $vars) { $addArgs += @("--variables", $v) }
Invoke-Railway $addArgs

# Volume and domain go through Railway's GraphQL API with the CLI's login:
# `railway volume add` panics on a service that has never been deployed.
function Invoke-RailwayApi([string]$Query) {
  $cfg = Get-Content (Join-Path $HOME ".railway/config.json") -Raw | ConvertFrom-Json
  $token = if ($cfg.user.accessToken) { $cfg.user.accessToken } else { $cfg.user.token }
  $body = @{ query = $Query } | ConvertTo-Json -Compress
  $res = Invoke-RestMethod -Method Post -Uri "https://backboard.railway.com/graphql/v2" `
    -Headers @{ Authorization = "Bearer $token" } -ContentType "application/json" `
    -Body ([Text.Encoding]::UTF8.GetBytes($body))
  if ($res.errors) { throw "Railway API: $($res.errors[0].message)" }
  $res.data
}

$status = (& $railway[0] @(@($railway | Select-Object -Skip 1) + @("status", "--json"))) -join "`n" | ConvertFrom-Json
$projectId = $status.id
$envNode = @($status.environments.edges | ForEach-Object { $_.node })
$environmentId = (@($envNode | Where-Object { $_.name -eq "production" }) + $envNode)[0].id
$serviceId = (Invoke-RailwayApi "{ project(id:`"$projectId`"){ services{ edges{ node{ id name } } } } }").project.services.edges |
  ForEach-Object { $_.node } | Where-Object { $_.name -eq $service } | Select-Object -First 1 -ExpandProperty id
if (-not $serviceId) { throw "No encuentro el servicio $service recién creado" }

Write-Host "==> Añadiendo volumen persistente en /data..."
Invoke-RailwayApi "mutation{ volumeCreate(input:{projectId:`"$projectId`", serviceId:`"$serviceId`", environmentId:`"$environmentId`", mountPath:`"/data`"}){ id } }" | Out-Null

Write-Host "==> Generando dominio público..."
$domain = ""
try {
  $domain = (Invoke-RailwayApi "mutation{ serviceDomainCreate(input:{serviceId:`"$serviceId`", environmentId:`"$environmentId`", targetPort:8000}){ domain } }").serviceDomainCreate.domain
} catch { Write-Host "  $_" -ForegroundColor Yellow }

if ($Local) {
  Write-Host "==> Subiendo el código y lanzando el despliegue..."
  Push-Location (Resolve-Path "$PSScriptRoot/../..")
  try { Invoke-Railway @("up", "--service", $service, "--detach") } finally { Pop-Location }
}

Write-Host ""
Write-Host "Instancia creada: $service" -ForegroundColor Green
Write-Host "  Clave de acceso : $apiKey"
if ($domain) {
  Write-Host "  Web             : https://$domain"
  Write-Host "  Vincular WhatsApp (envía este enlace a la persona):"
  Write-Host "    https://$domain/whatsapp#key=$apiKey" -ForegroundColor Cyan
} else {
  Write-Host "  No pude leer el dominio. Créalo en Railway > $service > Settings > Networking" -ForegroundColor Yellow
  Write-Host "  y abre https://<dominio>/whatsapp#key=$apiKey"
}
Write-Host ""
Write-Host "El primer despliegue tarda ~10-15 min (compila la imagen). Sigue el progreso con:"
Write-Host "  railway logs --service $service"
