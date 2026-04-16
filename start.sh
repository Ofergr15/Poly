#!/bin/bash
# Start the public trading dashboard server
cd "$(dirname "$0")"

echo "🚀 Starting Trading Dashboard on port 8080..."
echo "   Local:  http://localhost:8080"
echo ""
echo "   Make sure GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are set in .env"
echo "   Add http://localhost:8080/auth/callback as an authorized redirect URI"
echo "   in Google Cloud Console → APIs & Services → Credentials"
echo ""

python3 main.py
