# One-click minimal experiment: V0 vs V2 x 5 trials, then analyze.
# Prerequisites: apikey.txt + ref.png in this folder.
Set-Location $PSScriptRoot
python run_experiment.py 5 V0,V2
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python analyze.py
