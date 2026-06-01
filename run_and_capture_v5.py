"""Script to capture pytest output with verbose test names, excluding problematic tests."""
import subprocess
import sys

output_file = sys.argv[1]
extra_args = sys.argv[2:]
cmd = [
    "python", "-m", "pytest",
    "--tb=no", "-v", "-rN",
    "--continue-on-collection-errors",
    "--timeout=10",  # per-test timeout - short to avoid hangs
    "-p", "no:randomly",  # disable random ordering if present
    # Exclude tests that hang due to SSL/network issues or run pytest as subprocess
    "--ignore=tests/integration/test_existing_suite_still_green.py",  # meta-test that runs pytest
    "--ignore=tests/property/test_client_factory.py",  # SSL hang
    "--ignore=tests/property/test_credential_inject.py",  # SSL hang
    "--ignore=tests/property/test_health_contract.py",  # TestClient hang
] + extra_args

print(f"Running: {' '.join(cmd)}")
print(f"Output file: {output_file}")

result = subprocess.run(
    cmd,
    capture_output=True,
    text=True,
    cwd=r"C:\Users\ahmet\Desktop\yeni_atlassian\platform",
    timeout=600  # 10 minute overall timeout
)

combined = result.stdout + result.stderr
with open(output_file, "w", encoding="utf-8") as f:
    f.write(combined)

print(f"Captured {len(combined)} chars to {output_file}")
print(f"Return code: {result.returncode}")
# Print last 20 lines
lines = combined.strip().split("\n")
for line in lines[-20:]:
    print(line)
