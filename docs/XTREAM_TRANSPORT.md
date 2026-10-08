# Xtream playback transport

All Xtream Python sessions, curl calls, and FFmpeg HLS requests now use
`User-Agent: KSPlayer` by default. This agent was verified with Account 2 on
2026-10-08. Set `XTREAM_USER_AGENT` to override it (1–256 printable ASCII
characters). Ordinary application HTTP sessions are unaffected.

HLS starts from a private, random-path loopback playlist. FFmpeg copies its
HTTP options into nested playlists, redirects, and segments. The provider URL
stays out of process arguments and logs. The loopback listener closes with the
media child. The existing account lease, gate, and playback deadline still apply.

## Optional IPv6 bridge

The successful captured provider request used IPv6. Enable this only after
verifying an IPv6 internet route on the Docker host:

```sh
docker compose -f docker-compose.yml -f docker-compose.ipv6.yml up -d
```

The extra bridge retains container isolation and existing IPv4 access. Docker
27+ assigns an IPv6 ULA subnet automatically. The read-only `/etc/gai.conf`
mount makes the bridge's ULA source eligible for normal IPv6-first address
selection in Python, curl, and FFmpeg. This preference applies within Fruit's
container; it does not change the host or other apps. IPv4 remains available.

For a TrueNAS Custom App, preserve the saved app configuration, add the
`xtream_ipv6` network with `enable_ipv6: true`, attach Fruit to it alongside its
existing networks, and mount the same file from a persistent host checkout.
Use TrueNAS's `app.update` API rather than editing generated Compose files.

On the tested NAS, `ipv6_auto` was already enabled on `enp2s0`, but
`accept_ra=1` and forwarding enabled prevented learning a gateway. A reversible
`accept_ra=2` test obtained a global address and default route. Preserve this
through a TrueNAS SYSCTL tunable only after that live route check succeeds.
Keep existing forwarding enabled for Docker.

Validate a bounded real media GET and decoded video from Fruit after applying
both changes. An account metadata response or a reachable IPv6 socket alone
does not prove playback. Use Settings → Account Pool → Test Playback.

References: [Docker IPv6](https://docs.docker.com/engine/daemon/ipv6/),
[glibc address selection](https://man7.org/linux/man-pages/man5/gai.conf.5.html),
[Linux router advertisements](https://www.kernel.org/doc/html/v6.15/networking/ip-sysctl.html),
[TrueNAS tunables](https://www.truenas.com/docs/scale/24.10/scaletutorials/systemsettings/advanced/).
