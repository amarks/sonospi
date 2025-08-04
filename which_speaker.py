import soco

# Known speaker IPs
ips = [
    "192.168.4.34",
    "192.168.4.39",
    "192.168.4.144",
    "192.168.4.37",
    "192.168.4.35",
    "192.168.4.40",
    "192.168.4.36"
]

for ip in ips:
    s = soco.SoCo(ip)
    transport = s.get_current_transport_info()
    track_info = s.get_current_track_info()
    print(f"{ip} - {s.player_name}")
    print(f"  State: {transport['current_transport_state']}")
    print(f"  Title: {track_info['title']}")
    print(f"  Album Art: {track_info['album_art']}")
    print()
