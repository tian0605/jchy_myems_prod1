$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$mysqlBaseDirCandidates = @(
  'C:\Program Files\MySQL\MySQL Server 8.4',
  'C:\Program Files\MySQL\MySQL Server 8.3',
  'C:\Program Files\MySQL\MySQL Server 8.0'
)

$mysqlBaseDir = $mysqlBaseDirCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $mysqlBaseDir) {
  throw 'MySQL Server was not found. Install Oracle.MySQL first.'
}

$mysqlBinDir = Join-Path $mysqlBaseDir 'bin'
$mysqldExe = Join-Path $mysqlBinDir 'mysqld.exe'
$mysqlExe = Join-Path $mysqlBinDir 'mysql.exe'
$runtimeRoot = Join-Path $repoRoot '.local'
$mysqlRuntimeRoot = Join-Path $runtimeRoot 'mysql'
$dataDir = Join-Path $mysqlRuntimeRoot 'data'
$logDir = Join-Path $mysqlRuntimeRoot 'logs'
$uploadDir = Join-Path $repoRoot 'myems-admin\upload'
$dbPassword = '!MyEMS1'
$dbPort = 3306
$mysqlProcess = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -eq 'mysqld.exe' -and $_.CommandLine -like "*$dataDir*" } |
  Select-Object -First 1

foreach ($path in @($runtimeRoot, $mysqlRuntimeRoot, $dataDir, $logDir, $uploadDir)) {
  if (-not (Test-Path $path)) {
    New-Item -ItemType Directory -Path $path | Out-Null
  }
}

$tcpInUse = Get-NetTCPConnection -LocalPort $dbPort -ErrorAction SilentlyContinue
if ($tcpInUse -and -not $mysqlProcess) {
  throw "TCP port $dbPort is already in use. Free the port or update the bootstrap script to use another port."
}

$dataInitialized = Test-Path (Join-Path $dataDir 'mysql')
if (-not $dataInitialized) {
  & $mysqldExe --initialize-insecure --basedir=$mysqlBaseDir --datadir=$dataDir --console
}

$mysqlProcess = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -eq 'mysqld.exe' -and $_.CommandLine -like "*$dataDir*" } |
  Select-Object -First 1

if (-not $mysqlProcess) {
  $logFile = Join-Path $logDir 'mysqld.log'
  $startArguments = @(
    "--basedir=`"$mysqlBaseDir`"",
    "--datadir=`"$dataDir`"",
    "--port=$dbPort",
    '--bind-address=127.0.0.1',
    '--mysqlx=0',
    "--log-error=`"$logFile`""
  ) -join ' '
  $startInfo = New-Object System.Diagnostics.ProcessStartInfo
  $startInfo.FileName = $mysqldExe
  $startInfo.Arguments = $startArguments
  $startInfo.UseShellExecute = $false
  $startInfo.CreateNoWindow = $true
  [System.Diagnostics.Process]::Start($startInfo) | Out-Null
}

$maxAttempts = 60
for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
  try {
    & $mysqlExe --protocol=tcp --host=127.0.0.1 --port=$dbPort -u root -e "SELECT 1;" | Out-Null
    break
  }
  catch {
    if ($attempt -eq $maxAttempts) {
      throw 'MySQL did not become ready in time. Check .local/mysql/logs/mysqld.log'
    }
    Start-Sleep -Milliseconds 1000
  }
}

$connectionArguments = @('--protocol=tcp', '--host=127.0.0.1', "--port=$dbPort", '--default-character-set=utf8mb4', '-u', 'root')
$passwordConnectionArguments = $connectionArguments + @("--password=$dbPassword")
$isPasswordAlreadyConfigured = $false

try {
  & $mysqlExe @connectionArguments -e "SELECT 1;" | Out-Null
}
catch {
  & $mysqlExe @passwordConnectionArguments -e "SELECT 1;" | Out-Null
  $isPasswordAlreadyConfigured = $true
}

$grantSql = @"
ALTER USER 'root'@'localhost' IDENTIFIED BY '$dbPassword';
CREATE USER IF NOT EXISTS 'root'@'127.0.0.1' IDENTIFIED BY '$dbPassword';
GRANT ALL PRIVILEGES ON *.* TO 'root'@'127.0.0.1' WITH GRANT OPTION;
FLUSH PRIVILEGES;
"@

if (-not $isPasswordAlreadyConfigured) {
  & $mysqlExe @connectionArguments -e $grantSql
}

$installSqlFiles = @(
  'database/install/myems_system_db.sql',
  'database/install/myems_historical_db.sql',
  'database/install/myems_energy_db.sql',
  'database/install/myems_billing_db.sql',
  'database/install/myems_carbon_db.sql',
  'database/install/myems_energy_baseline_db.sql',
  'database/install/myems_energy_model_db.sql',
  'database/install/myems_energy_plan_db.sql',
  'database/install/myems_energy_prediction_db.sql',
  'database/install/myems_fdd_db.sql',
  'database/install/myems_user_db.sql',
  'database/install/myems_reporting_db.sql',
  'database/install/myems_production_db.sql',
  'database/demo-en/myems_system_db.sql'
)

foreach ($relativeSqlPath in $installSqlFiles) {
  $sqlPath = Join-Path $repoRoot $relativeSqlPath
  $normalizedSqlPath = $sqlPath.Replace('\\', '/')
  & $mysqlExe @passwordConnectionArguments --execute="source $normalizedSqlPath"
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to import SQL file: $relativeSqlPath"
  }
}

$apiEnvContent = @"
# config for myems_system_db
MYEMS_SYSTEM_DB_HOST=127.0.0.1
MYEMS_SYSTEM_DB_PORT=$dbPort
MYEMS_SYSTEM_DB_DATABASE=myems_system_db
MYEMS_SYSTEM_DB_USER=root
MYEMS_SYSTEM_DB_PASSWORD=$dbPassword

# config for myems_energy_db
MYEMS_ENERGY_DB_HOST=127.0.0.1
MYEMS_ENERGY_DB_PORT=$dbPort
MYEMS_ENERGY_DB_DATABASE=myems_energy_db
MYEMS_ENERGY_DB_USER=root
MYEMS_ENERGY_DB_PASSWORD=$dbPassword

# config for myems_energy_baseline_db
MYEMS_ENERGY_BASELINE_DB_HOST=127.0.0.1
MYEMS_ENERGY_BASELINE_DB_PORT=$dbPort
MYEMS_ENERGY_BASELINE_DB_DATABASE=myems_energy_baseline_db
MYEMS_ENERGY_BASELINE_DB_USER=root
MYEMS_ENERGY_BASELINE_DB_PASSWORD=$dbPassword

# config for myems_energy_plan_db
MYEMS_ENERGY_PLAN_DB_HOST=127.0.0.1
MYEMS_ENERGY_PLAN_DB_PORT=$dbPort
MYEMS_ENERGY_PLAN_DB_DATABASE=myems_energy_plan_db
MYEMS_ENERGY_PLAN_DB_USER=root
MYEMS_ENERGY_PLAN_DB_PASSWORD=$dbPassword

# config for myems_energy_prediction_db
MYEMS_ENERGY_PREDICTION_DB_HOST=127.0.0.1
MYEMS_ENERGY_PREDICTION_DB_PORT=$dbPort
MYEMS_ENERGY_PREDICTION_DB_DATABASE=myems_energy_prediction_db
MYEMS_ENERGY_PREDICTION_DB_USER=root
MYEMS_ENERGY_PREDICTION_DB_PASSWORD=$dbPassword

# config for myems_billing_db
MYEMS_BILLING_DB_HOST=127.0.0.1
MYEMS_BILLING_DB_PORT=$dbPort
MYEMS_BILLING_DB_DATABASE=myems_billing_db
MYEMS_BILLING_DB_USER=root
MYEMS_BILLING_DB_PASSWORD=$dbPassword

# config for myems_historical_db
MYEMS_HISTORICAL_DB_HOST=127.0.0.1
MYEMS_HISTORICAL_DB_PORT=$dbPort
MYEMS_HISTORICAL_DB_DATABASE=myems_historical_db
MYEMS_HISTORICAL_DB_USER=root
MYEMS_HISTORICAL_DB_PASSWORD=$dbPassword

# config for myems_production_db
MYEMS_PRODUCTION_DB_HOST=127.0.0.1
MYEMS_PRODUCTION_DB_PORT=$dbPort
MYEMS_PRODUCTION_DB_DATABASE=myems_production_db
MYEMS_PRODUCTION_DB_USER=root
MYEMS_PRODUCTION_DB_PASSWORD=$dbPassword

# config for myems_user_db
MYEMS_USER_DB_HOST=127.0.0.1
MYEMS_USER_DB_PORT=$dbPort
MYEMS_USER_DB_DATABASE=myems_user_db
MYEMS_USER_DB_USER=root
MYEMS_USER_DB_PASSWORD=$dbPassword

# config for myems_fdd_db
MYEMS_FDD_DB_HOST=127.0.0.1
MYEMS_FDD_DB_PORT=$dbPort
MYEMS_FDD_DB_DATABASE=myems_fdd_db
MYEMS_FDD_DB_USER=root
MYEMS_FDD_DB_PASSWORD=$dbPassword

# config for myems_reporting_db
MYEMS_REPORTING_DB_HOST=127.0.0.1
MYEMS_REPORTING_DB_PORT=$dbPort
MYEMS_REPORTING_DB_DATABASE=myems_reporting_db
MYEMS_REPORTING_DB_USER=root
MYEMS_REPORTING_DB_PASSWORD=$dbPassword

# config for myems_carbon_db
MYEMS_CARBON_DB_HOST=127.0.0.1
MYEMS_CARBON_DB_PORT=$dbPort
MYEMS_CARBON_DB_DATABASE=myems_carbon_db
MYEMS_CARBON_DB_USER=root
MYEMS_CARBON_DB_PASSWORD=$dbPassword

IS_REDIS_ENABLED=False
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_PASSWORD=
REDIS_DB=0

MINUTES_TO_COUNT=60
UTC_OFFSET=+08:00
WORKING_DAY_START_TIME_LOCAL=00:00:00
UPLOAD_PATH=$($uploadDir.Replace('\\', '/'))/
CURRENCY_UNIT=CNY
MAXIMUM_FAILED_LOGIN_COUNT=3
IS_TARIFF_APPENDED=True
IS_RECURSIVE=True
SESSION_EXPIRES_IN_SECONDS=28800
"@

Set-Content -Path (Join-Path $repoRoot 'myems-api/.env') -Value $apiEnvContent -Encoding ASCII

$serviceEnvTargets = @(
  'myems-aggregation/.env',
  'myems-cleaning/.env',
  'myems-normalization/.env',
  'myems-modbus-tcp/.env',
  'myems-mqtt/.env'
)

foreach ($relativeEnvTarget in $serviceEnvTargets) {
  $targetPath = Join-Path $repoRoot $relativeEnvTarget
  $sourcePath = Join-Path $repoRoot ($relativeEnvTarget -replace '/\.env$', '/example.env')
  $content = Get-Content -Raw $sourcePath
  $content = $content -replace 'MYEMS_SYSTEM_DB_PORT=3306', "MYEMS_SYSTEM_DB_PORT=$dbPort"
  $content = $content -replace 'MYEMS_HISTORICAL_DB_PORT=3306', "MYEMS_HISTORICAL_DB_PORT=$dbPort"
  $content = $content -replace 'MYEMS_ENERGY_DB_PORT=3306', "MYEMS_ENERGY_DB_PORT=$dbPort"
  $content = $content -replace 'MYEMS_BILLING_DB_PORT=3306', "MYEMS_BILLING_DB_PORT=$dbPort"
  $content = $content -replace 'MYEMS_CARBON_DB_PORT=3306', "MYEMS_CARBON_DB_PORT=$dbPort"
  Set-Content -Path $targetPath -Value $content -Encoding ASCII
}

Write-Host 'Bootstrap complete.'
Write-Host 'Database:' "127.0.0.1:$dbPort"
Write-Host 'API env:' (Join-Path $repoRoot 'myems-api/.env')
Write-Host 'Default Web/Admin login: administrator / !MyEMS1'