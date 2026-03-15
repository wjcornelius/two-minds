# Kill all Chloe and Faith agent windows
$targets = Get-Process cmd -ErrorAction SilentlyContinue | Where-Object {
    $_.MainWindowTitle -match 'Chloe|Faith'
}
foreach ($p in $targets) {
    Write-Host "Killing: $($p.MainWindowTitle) (PID $($p.Id))"
    Stop-Process -Id $p.Id -Force
}
if (-not $targets) { Write-Host "No Chloe/Faith cmd windows found" }

# Also kill any orphaned agent.py python processes
# Remove stale locks
Remove-Item "C:\Users\wjcor\OneDrive\Desktop\Offspring\data\agent.lock" -ErrorAction SilentlyContinue
Remove-Item "C:\Users\wjcor\OneDrive\Desktop\Offspring\data_faith\agent.lock" -ErrorAction SilentlyContinue
Write-Host "Lock files cleared"
