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
  [string]$AnthropicKey = ""
)

$ErrorActionPreference = "Stop"

$railway = if (Get-Command railway -ErrorAction SilentlyContinue) { @("railway") } else { @("npx", "-y", "@railway/cli@latest") }
function Invoke-Railway {
  $exe = $railway[0]
  $rest = @($railway | Select-Object -Skip 1) + $args
  & $exe @rest
  if ($LASTEXITCODE -ne 0) { throw "railway $($args -join ' ') falló (código $LASTEXITCODE)" }
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
  "OPENJARVIS_API_KEY=$apiKey",
  "JARVIS_MODEL=$Model",
  "JARVIS_ASSISTANT_NAME=$AssistantName",
  "WHATSAPP_MODE=$WhatsAppMode",
  "OPENAI_API_KEY=$openai",
  "ANTHROPIC_API_KEY=$anthropic"
)
if ($AllowedNumbers) { $vars += "WHATSAPP_ALLOWED_NUMBERS=$AllowedNumbers" }

Write-Host "==> Creando servicio $service desde $Repo ($Branch)..."
$addArgs = @("add", "--service", $service, "--repo", $Repo, "--branch", $Branch)
foreach ($v in $vars) { $addArgs += @("--variables", $v) }
Invoke-Railway @addArgs

Write-Host "==> Añadiendo volumen persistente en /data..."
Invoke-Railway volume --service $service add --mount-path /data

Write-Host "==> Generando dominio público..."
$domainJson = (& $railway[0] @(@($railway | Select-Object -Skip 1) + @("domain", "--service", $service, "--port", "8000", "--json"))) -join "`n"
$domain = ""
if ($domainJson -match "([a-z0-9-]+\.up\.railway\.app)") { $domain = $Matches[1] }

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
