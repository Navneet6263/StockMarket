import os
import sys
import webbrowser
from urllib.parse import urlparse, parse_qs
from fyers_apiv3 import fyersModel

# Ask the user for their App ID and Secret Key
print("="*60)
print("FYERS API V3 - ACCESS TOKEN GENERATOR")
print("="*60)
print("\n[Step 1] Find your App ID and Secret Key from the Fyers Developer Console (https://myapi.fyers.in/)")

try:
    client_id = input("Enter your App ID (e.g., ABCD12345-100): ").strip()
    secret_key = input("Enter your Secret Key (e.g., XYZ98765): ").strip()
except KeyboardInterrupt:
    print("\nAborted.")
    sys.exit(1)

if not client_id or not secret_key:
    print("Error: App ID and Secret Key are required.")
    sys.exit(1)

# App configuration
redirect_uri = "https://127.0.0.1:8080/callback"
response_type = "code"
state = "sample_state"
grant_type = "authorization_code"

# Create a session model with the provided credentials
session = fyersModel.SessionModel(
    client_id=client_id,
    secret_key=secret_key,
    redirect_uri=redirect_uri,
    response_type=response_type,
    grant_type=grant_type,
    state=state
)

# Generate the auth code URL
auth_url = session.generate_authcode()

print("\n[Step 2] Authenticate with Fyers")
print(f"Opening browser to: {auth_url}")
print("Please login to your Fyers account and authorize the app.")
print("After authorization, you will be redirected to an error page (127.0.0.1 refused to connect). THIS IS NORMAL.")

try:
    webbrowser.open(auth_url)
except Exception:
    pass

print("\n[Step 3] Extract the auth_code from the URL")
print("Look at the URL in your browser's address bar after redirect.")
print("It will look like: https://127.0.0.1:8080/callback?auth_code=XXXXXXXX&state=sample_state")
try:
    redirected_url = input("\nPaste the ENTIRE URL here (or just the auth_code): ").strip()
except KeyboardInterrupt:
    print("\nAborted.")
    sys.exit(1)

# Extract auth_code from URL if they pasted the whole URL
auth_code = redirected_url
if "http" in redirected_url:
    try:
        parsed_url = urlparse(redirected_url)
        parsed_qs = parse_qs(parsed_url.query)
        if "auth_code" in parsed_qs:
            auth_code = parsed_qs["auth_code"][0]
        else:
            print("Error: Could not find auth_code in the URL.")
            sys.exit(1)
    except Exception as e:
        print(f"Error parsing URL: {e}")
        sys.exit(1)

print(f"\nAuth code extracted: {auth_code}")

print("\n[Step 4] Generating Access Token...")
session.set_token(auth_code)

try:
    response = session.generate_token()
    if response.get("s") == "ok":
        access_token = response["access_token"]
        print("\n✅ SUCCESS! Access Token generated successfully.")
        
        # Save to .env file in the same directory
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        
        # Read existing .env if it exists
        env_lines = []
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                env_lines = f.readlines()
                
        # Update or append FYERS_ACCESS_TOKEN
        token_written = False
        for i, line in enumerate(env_lines):
            if line.startswith("FYERS_ACCESS_TOKEN="):
                env_lines[i] = f"FYERS_ACCESS_TOKEN={access_token}\n"
                token_written = True
                break
                
        if not token_written:
            env_lines.append(f"\nFYERS_ACCESS_TOKEN={access_token}\n")
            
        with open(env_path, "w") as f:
            f.writelines(env_lines)
            
        print(f"\nToken has been saved to: {env_path}")
        print("The system will now use Fyers for live Options Data! 🎉")
    else:
        print("\n❌ FAILED to generate access token.")
        print(f"Fyers Response: {response}")
except Exception as e:
    print(f"\n❌ FAILED with exception: {e}")
