#!/bin/bash
# Deploy REWE Dashboard
# Run from the Rewe project directory

set -e

echo "=== REWE Dashboard Deploy ==="
echo ""

# 1. Parse new PDFs
echo "[1/4] Parsing PDFs..."
python3 rewe_parser.py

# 2. Build report
echo "[2/4] Building report..."
python3 build_report.py

# 3. Copy to hosting folder
echo "[3/4] Copying to wix-hosting..."
cp report.html wix-hosting/

# 4. Generate password hash if needed
echo "[4/4] Done!"
echo ""
echo "Files ready in wix-hosting/"
echo "  - login.html (password page)"
echo "  - report.html (dashboard)"
echo ""
echo "To set your password, run:"
echo "  python3 -c \"import hashlib; print(hashlib.sha256(b'YOUR_PASSWORD').hexdigest())\""
echo "  Then update HASH in login.html"
echo ""
echo "To deploy to Netlify:"
echo "  cd wix-hosting && netlify deploy --prod --dir=."
echo ""
echo "To deploy to GitHub Pages:"
echo "  cd wix-hosting && git add . && git commit -m 'Update dashboard' && git push"
