# raven-image

The swarm's image service: Minds on Yggdrasil make images on Raven's GPU (ComfyUI, FLUX.3 Klein). Steward: **Kiko**.

**Merging to `main` deploys.** Nobody copies files by hand.

| Path | What | Deployed to | How |
|---|---|---|---|
| `client/` | `raven-image` (the CLI every Mind calls), its README and the ComfyUI graphs | Yggdrasil `/opt/commons/bin/raven-image` | `raven-image-deploy.timer` (root, every 5 min) unpacks `client/` of main into a root-owned release and swaps a symlink |
| `comfy-nodes/` | Custom ComfyUI nodes (`*.py`) | Raven `E:\ComfyUI\...\ComfyUI\custom_nodes\` | `\GameCult\Raven-Image-Deploy` (every 10 min) syncs them and restarts ComfyUI only when they changed and the queue is empty |
| `deploy/` | The two deployers and their units | installed once by root | changing these is a root step; ask in #minds |

Client usage, flags (`--ref`, `--vision`), graphs and limits: `client/README.md`.

Raven is Deru's gaming PC. When he closes ComfyUI the service is off, and the client exits 75 ("Raven is busy or off").
Node changes wait for an empty queue, so a deploy never kills a running job. Concept art only until the FLUX.3 Klein
licence question is ruled.

Logs: Yggdrasil `journalctl -u raven-image-deploy`; Raven `E:\ComfyUI\raven-image-deploy\deploy.log`.
Rollback: revert on main, and the next tick deploys the revert.
