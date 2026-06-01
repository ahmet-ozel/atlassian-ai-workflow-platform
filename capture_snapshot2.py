"""Script to capture pytest output to a file."""
import subprocess
import sys

output_file = sys.argv[1]
extra_args = sys.argv[2:]
cmd = ["python", "-m", "pytest", "--tb=no", "-q", "-rN", "--continue-on-collection-errors"] + extra_args

result = subprocess.run(
    cmd,
    capture_output=True,
    text=True,
    cwd=r"C:\Users\ahmet\Desktop\yeni_atlassian\platform"
)

combined = result.stdout + result.stderr
with open(output_file, "w", encoding="utf-8") as f:
    f.write(combined)

print(f"Captured {len(combined)} chars to {output_file}")
print(f"Return code: {result.returncode}")
# Print last 30 lines
lines = combined.strip().split("\n")
for line in lines[-30:]:
    print(line)
