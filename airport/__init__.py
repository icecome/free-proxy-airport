"""free-proxy-airport business library.

Modules are grouped by pipeline stage; every one of them is runnable as a CLI:

    collect  -> generator.py        fetch sources, benchmark, publish subscription
    probe    -> cn_alive_check.py   local TCP reachability over a subscription
    verify   -> verify_nodes.py     sing-box tunnel + real egress verification
                local_verify.py     mihomo batch health/speed verification
                rescue_pass.py      second opinion on rejected nodes
    ledger   -> node_registry.py    time-bound verified/blacklist store
                apply_verdicts.py   reports -> registry
    publish  -> publish_verified.py registry -> subscription
    nas      -> nas_probe.py        NAS-side CN vantage probe loop

Entry points (both forms work from any directory):

    python -m airport.generator
    python airport/generator.py

Paths are resolved through `airport.paths`, never through the current working
directory — see documentation/structure.md for the layout contract.
"""
