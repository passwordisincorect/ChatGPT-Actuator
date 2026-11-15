"""Local activation and authenticated remote tool proofs are independent states."""
def states(integrity,local_mcp,openai,tailscale):
    deployed=bool(integrity and local_mcp)
    def link(value):return 'verified' if value=='verified' else 'stale' if value=='stale' else 'awaiting_remote_call'
    links={'openai':link(openai),'tailscale':link(tailscale)}
    verified=deployed and all(v=='verified' for v in links.values())
    return {'deployment':'applied' if deployed else 'unverified','connections':'verified' if verified else 'awaiting_remote_calls','links':links,'complete':verified}
