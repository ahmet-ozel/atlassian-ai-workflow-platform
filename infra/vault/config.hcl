# =============================================================================
# infra/vault/config.hcl — Vault production server configuration (Y2)
# =============================================================================
# Used by ``docker-compose.prod.yml`` to bring Vault up in production mode
# instead of the in-memory ``-dev`` mode shipped by the base compose.
#
# Storage: ``raft`` (integrated storage) — single-node by default, can
# be extended to a 3-node cluster by adding peer node_id entries.
# Persistent state lives at ``/vault/data`` (the named volume
# ``vault_data`` declared in docker-compose.prod.yml) so restarting the
# container preserves secrets instead of wiping credentials on restart
# like Vault ``-dev`` mode.
#
# Init/unseal flow (one-time per fresh deployment):
#
#   1. make prod                            # start the stack
#   2. docker compose -p platform exec vault \
#        vault operator init -key-shares=5 -key-threshold=3
#      → records 5 unseal keys + 1 root token. Distribute the unseal
#        keys to separate trusted operators; archive the root token in
#        the org's break-glass vault.
#   3. docker compose -p platform exec vault vault operator unseal <k1>
#      (repeat for keys 2 and 3 of the 3-of-5 threshold)
#   4. The dashboard's vault_init router (services/admin-dashboard-api/
#      src/routers/vault_init.py) exposes a UI flow that can drive
#      steps 2-3 if operators prefer a browser to the CLI.
#
# After every container restart only step 3 (unseal) repeats.
# Operators can opt into auto-unseal by adding a ``seal`` stanza
# pointing at AWS KMS / Azure Key Vault / GCP KMS — see Vault docs.
# =============================================================================

ui = true

listener "tcp" {
  address     = "0.0.0.0:8200"
  # TLS termination handled by Traefik in front of Vault; the listener
  # itself runs cleartext on the internal network. For production
  # without Traefik, swap to ``tls_cert_file`` / ``tls_key_file`` here.
  tls_disable = true
}

storage "raft" {
  path    = "/vault/data"
  node_id = "vault-node-1"
}

# Cluster + API addresses — Vault uses these for raft peer communication
# and for clients to discover the active node after a leadership change.
api_addr     = "http://vault:8200"
cluster_addr = "http://vault:8201"

# Disable mlock when the container's filesystem doesn't support it
# (common with overlay / docker volumes). For production hosts that DO
# support mlock, flip this to ``false`` so secrets can't be swapped to
# disk.
disable_mlock = true
