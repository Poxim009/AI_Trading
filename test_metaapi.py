"""
test_metaapi.py — Smoke test script to verify MetaApi cloud connectivity
"""
import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv()

async def main():
    token = os.environ.get("METAAPI_TOKEN")
    account_id = os.environ.get("METAAPI_ACCOUNT_ID")
    
    if not token or token == "your_metaapi_token_here":
        print("❌ METAAPI_TOKEN not configured in .env")
        return False
    if not account_id or account_id == "your_metaapi_account_id_here":
        print("❌ METAAPI_ACCOUNT_ID not configured in .env")
        return False

    print(f"🔑 Testing MetaApi connection for Account ID: {account_id}...")
    try:
        from metaapi_cloud_sdk import MetaApi
    except ImportError:
        print("❌ metaapi-cloud-sdk is not installed")
        return False

    api = MetaApi(token=token)
    try:
        account = await api.metatrader_account_api.get_account(account_id)
        print(f"📡 Account State: {account.state}")
        
        if account.state != "DEPLOYED":
            print("🔄 Deploying account on cloud server...")
            await account.deploy()
            print("✅ Account deployed!")
            
        print("⏳ Establishing RPC connection and synchronizing...")
        conn = account.get_rpc_connection()
        await conn.connect()
        await conn.wait_synchronized()
        
        acc_info = await conn.get_account_information()
        server = acc_info.get("server", "Cloud")
        balance = float(acc_info.get("balance", 0.0) or 0.0)
        equity = float(acc_info.get("equity", 0.0) or 0.0)
        print(f"✅ CONNECTED! Server: {server} | Balance: ${balance:,.2f} | Equity: ${equity:,.2f}")
        
        price = await conn.get_symbol_price("XAUUSD")
        bid = float(price.get("bid", 0.0) or 0.0)
        ask = float(price.get("ask", 0.0) or 0.0)
        print(f"📊 Live XAUUSD Price: Bid = {bid:,.2f} | Ask = {ask:,.2f}")
        
        await conn.close()
        print("🎉 MetaApi Cloud Verification PASSED 100%!")
        return True
    except Exception as e:
        print(f"❌ MetaApi Error: {e}")
        return False

if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
