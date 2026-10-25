# Local simulation test pack

This pack demonstrates one publication job against the local FastAPI service in its default simulation mode. It does not publish to an external service.

## Run

1. Start the API from the repository root:

   ```powershell
   python -m uvicorn app.main:app
   ```

2. Create the input destination through the API (the request uses development-only rules):

   ```powershell
   $body = @{
     key = "demo-community"
     connector_key = "discourse"
     config = @{}
     rules = @{
       frequency_cap = 5
       frequency_window_seconds = 604800
       self_promo_allowed = $false
       link_policy = "allow"
       disclosure_required = $false
     }
   } | ConvertTo-Json
   Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/destinations `
     -Headers @{"Idempotency-Key" = [guid]::NewGuid().ToString()} `
     -ContentType "application/json" -Body $body
   ```

3. Submit the fixture and collect its final status, result, evidence, and runner log:

   ```powershell
   python scripts/run_batch.py test_pack/inputs/publish-demo.json
   ```

The runner writes one job JSON under `test_pack/outputs/` and a log at `test_pack/logs/batch-runner.log`. The checked-in sample output and log are from an actual local simulation run; the evidence explicitly records `simulation_mode: true` and `is_real_publication: false`. Re-running creates a new job ID and output file. See [index.json](index.json) for the recorded test-pack contents.
