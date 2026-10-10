"""Run an isolated named Tailscale gateway profile."""
import argparse
import asyncio
import os
from pathlib import Path

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--profiles",required=True,type=Path)
    parser.add_argument("--profile",required=True)
    args=parser.parse_args()
    os.environ["CHATGPT_ACTUATOR_PROFILE_FILE"]=str(args.profiles.resolve())
    os.environ["CHATGPT_ACTUATOR_PROFILE"]=args.profile
    import gateway
    asyncio.run(gateway.run())

if __name__=="__main__":
    main()
