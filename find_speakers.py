import soco

devices = soco.discover()
if not devices:
    print("No Sonos devices found.")
else:
    for d in devices:
        info = d.get_current_transport_info()
        state = info.get("current_transport_state", "UNKNOWN")
        print(f"{d.ip_address:>15}  {d.player_name:<20}  State: {state}")
