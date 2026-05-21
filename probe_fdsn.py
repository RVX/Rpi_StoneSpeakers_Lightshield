from obspy.clients.fdsn import Client
from obspy import UTCDateTime

c = Client("https://service.earthscope.org", timeout=60)
t2 = UTCDateTime.now() - 1800
t1 = t2 - 600

# 1) Inventory probe - list available channels for IU.PET
try:
    inv = c.get_stations(network="IU", station="PET", level="channel",
                         starttime=t1, endtime=t2)
    print("=== Inventory for IU.PET ===")
    for net in inv:
        for sta in net:
            for ch in sta:
                print(f"  {net.code}.{sta.code}.{ch.location_code}.{ch.code}  "
                      f"sr={ch.sample_rate}  {ch.start_date} -> {ch.end_date}")
except Exception as e:
    print("Inventory failed:", e)

print()

# 2) Try various stations/locations to find one with current data
candidates = [
    ("IU", "PET", "00", "BHZ"),
    ("IU", "PET", "10", "BHZ"),
    ("IU", "PET", "", "BHZ"),
    ("IU", "MAJO", "00", "BHZ"),
    ("IU", "RABL", "00", "BHZ"),
    ("IU", "SNZO", "00", "BHZ"),
    ("IU", "HNR", "00", "BHZ"),
    ("IU", "DAV", "00", "BHZ"),
]
for net, sta, loc, ch in candidates:
    try:
        st = c.get_waveforms(net, sta, loc, ch, t1, t2)
        print(f"OK   {net}.{sta}.{loc!r}.{ch}  ->  {len(st)} trace(s), "
              f"{sum(len(tr) for tr in st)} samples")
    except Exception as e:
        print(f"FAIL {net}.{sta}.{loc!r}.{ch}  ->  {str(e)[:60]}")
