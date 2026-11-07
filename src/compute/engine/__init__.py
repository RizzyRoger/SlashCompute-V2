"""LLM inference across the pool: llama.cpp RPC splits one GGUF model's layers over several computers.

The coordinator side mounts under :data:`PREFIX` on the main server (plus ``/v1/*`` at the root);
each contributing computer runs ``compute node``.
"""

PREFIX = "/inference"
