"""Start the existing Actuator backend with validated loopback HTTP settings."""
import argparse
import os
from pathlib import Path
from urllib.parse import urlsplit
from gateway_config import load_profile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--profiles', required=True, type=Path)
    parser.add_argument('--profile', required=True)
    args = parser.parse_args()
    profile = load_profile(args.profiles, args.profile)
    backend = urlsplit(profile['actuator_backend'])
    os.environ['ACTUATOR_MCP_TRANSPORT'] = 'http'
    os.environ['ACTUATOR_MCP_HOST'] = backend.hostname
    os.environ['ACTUATOR_MCP_PORT'] = str(backend.port)
    os.environ['CHATGPT_ACTUATOR_ADMIN_PORT'] = str(profile['actuator_admin_port'])
    from chatgpt_actuator.server import main as serve
    serve()


if __name__ == '__main__':
    main()
