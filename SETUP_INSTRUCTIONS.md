# Setup Instructions for NSE Options API

## Quick Fix for Options Data

Your NSE Options API is currently using fallback data. Follow these steps to get REAL data:

### Step 1: Install NSEPython Library

Open terminal in project root and run:

```bash
# Activate virtual environment first
.venv\Scripts\activate

# Install nsepython
pip install nsepython

# Or reinstall all requirements
pip install -r services/api/requirements.txt
```

### Step 2: Restart Backend

```bash
# Stop the current API server (Ctrl+C)
# Then restart
npm run dev:api
```

### Step 3: Test Options

1. Open browser: http://localhost:3000
2. Enter symbol: **NIFTY** or **BANKNIFTY**
3. Click "📊 Options Chain Analysis" button
4. Check terminal logs - should see "NSEPython success!"

---

## Why NSEPython?

- **Free & Open Source** - No API key needed
- **Handles NSE cookies automatically** - No manual session management
- **More reliable** - Built specifically for NSE
- **Active maintenance** - Regular updates

---

## Troubleshooting

### If still showing fallback data:

1. **Check Internet Connection** - NSE API needs internet
2. **Market Hours** - Works best during market hours (9:15 AM - 3:30 PM IST)
3. **Firewall/Antivirus** - May block NSE requests
4. **VPN** - Try disabling VPN if enabled

### Check Terminal Logs:

Look for these messages:
- ✅ "NSEPython success! Got data for NIFTY"
- ❌ "NSEPython failed: [error]"
- ❌ "Timeout fetching options"

---

## Alternative: Use Fallback Data

If you want to continue with demo/fallback data (for testing):
- No action needed
- Fallback data is realistic and good for UI testing
- Shows proper structure and calculations

---

## Production Deployment

For production with real NSE data:

1. Install nsepython: `pip install nsepython`
2. Consider caching options data (updates every 1-2 minutes)
3. Add retry logic for failed requests
4. Monitor NSE API availability

---

## Need Help?

Check backend terminal for detailed error logs when clicking Options button.
