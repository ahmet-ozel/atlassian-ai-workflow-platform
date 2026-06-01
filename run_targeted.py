"""Script to capture pytest output for specific test directories."""
import subprocess
import sys

output_file = sys.argv[1]
test_paths = sys.argv[2:]

cmd = [
    "python", "-m", "pytest",
    "--tb=no", "-v", "-rN",
    "--continue-on-collection-errors",
    "--timeout=30",  # per-test timeout
    "-p", "no:randomly",
] + test_paths

print(f"Running: {' '.join(cmd)}")
print(f"Output file: {output_file}")

result = subprocess.run(
    cmd,
    capture_output=True,
    text=True,
    cwd=r"C:\Users\ahmet\Desktop\yeni_atlassian\platform",
    timeout=300  # 5 minute overall timeout
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
