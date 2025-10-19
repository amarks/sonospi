# Utility to discover all Sonos devices on the network. Just used for diagnostics and troubleshooting.  
#!/usr/bin/env python3
import soco

def iter_groups(timeout=5):
    zones = soco.discover(timeout=timeout) or set()
    # unique Group objects
    return sorted({z.group for z in zones}, key=lambda g: g.uid)

def main():
    for g in iter_groups():
        coord = g.coordinator
        # Deduplicate members by UID, then present names
        members = sorted({(p.uid, p.player_name) for p in g.members}, key=lambda x: x[1])
        member_names = ", ".join(name for _, name in members)

        transport = coord.get_current_transport_info()
        track = coord.get_current_track_info()

        print(f"Group: {member_names}")
        print(f"  Coordinator: {coord.player_name} ({coord.ip_address})")
        print(f"  State: {transport.get('current_transport_state')}")
        print(f"  Title: {track.get('title')}")
        print(f"  Album Art: {track.get('album_art')}\n")

if __name__ == "__main__":
    main()

