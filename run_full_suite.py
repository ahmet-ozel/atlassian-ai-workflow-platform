"""Script to capture full platform test suite output."""
import subprocess
import sys

output_file = sys.argv[1]

cmd = [
    "python", "-m", "pytest",
    "--tb=no", "-v", "-rN",
    "--continue-on-collection-errors",
    "--timeout=30",  # per-test timeout
    "-p", "no:randomly",
    # Exclude tests that hang due to SSL/network issues or run pytest as subprocess
    "--ignore=tests/integration/test_existing_suite_still_green.py",  # meta-test that runs pytest
    "--ignore=tests/property/test_client_factory.py",  # SSL hang
    "--ignore=tests/property/test_credential_inject.py",  # SSL hang
    "--ignore=tests/property/test_health_contract.py",  # TestClient hang
    "--ignore=tests/property/test_llm_call_paths.py",  # file scan hang
    "--ignore=tests/property/test_llm_retry_fallback.py",  # asyncio hang
    "--ignore=tests/property/test_path_coverage.py",  # file scan hang
]

print(f"Running: {' '.join(cmd)}")
print(f"Output file: {output_file}")

result = subprocess.run(
    cmd,
    capture_output=True,
    text=True,
    cwd=r"C:\Users\ahmet\Desktop\yeni_atlassian\platform",
    timeout=2400  # 40 minute overall timeout (extended for after-fix run)
)

combined = result.stdout + result.stderr
with open(output_file, "w", encoding="utf-8") as f:
    f.write(combined)

print(f"Captured {len(combined)} chars to {output_file}")
print(f"Return code: {result.returncode}")
# Print last 10 lines
lines = combined.strip().split("\n")
for line in lines[-10:]:
    print(line)
