import soco

# Utility to discover all Sonos devices on the network. Just used for diagnostics and troubleshooting.  
devices = soco.discover()

if not devices:
    print("No Sonos devices found.")
else:
    for s in devices:
        try:
            transport = s.get_current_transport_info()
            track_info = s.get_current_track_info()
            print(f"{s.ip_address} - {s.player_name}")
            print(f"  State: {transport['current_transport_state']}")
            print(f"  Title: {track_info['title']}")
            print(f"  Album Art: {track_info['album_art']}")
            print()
        except Exception as e:
            print(f"{s.ip_address} - {s.player_name} - Error: {e}")