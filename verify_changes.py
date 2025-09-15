#!/usr/bin/env python3
"""
Quick syntax verification for the production review fixes
"""

import ast
import sys
import traceback

def check_python_syntax(filename):
    """Check if a Python file has valid syntax"""
    try:
        with open(filename, 'r') as f:
            source = f.read()
        ast.parse(source)
        return True, None
    except SyntaxError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)

files_to_check = [
    'ee/hogai/session_summaries/session/output_data.py',
    'ee/hogai/graph/session_summaries/nodes.py'
]

print("Checking syntax of modified files...")
all_passed = True

for filename in files_to_check:
    success, error = check_python_syntax(filename)
    if success:
        print(f"✅ {filename} - Syntax OK")
    else:
        print(f"❌ {filename} - Syntax Error: {error}")
        all_passed = False

if all_passed:
    print("\n🎉 All files passed syntax validation!")
    sys.exit(0)
else:
    print("\n💥 Some files have syntax errors!")
    sys.exit(1)