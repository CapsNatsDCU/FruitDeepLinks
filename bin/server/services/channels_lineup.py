"""Channels DVR exports composed from existing persistent and lane exporters."""
from __future__ import annotations

from decimal import Decimal
import hashlib
from xml.etree import ElementTree as ET

from db.preferences import get_setting
from fruit_export_lanes import lanes_xmltv_tree
from server.services.xtream_persistent import list_channels, render_m3u, xmltv_tree
from xtream_accounts import load_accounts, safe_value
from xtream_pool_schema import ensure_schema


def lineup(conn):
    ensure_schema(conn)
    persistent = list_channels(conn, enabled_only=True)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    lanes = [dict(r) for r in conn.execute("SELECT * FROM lanes ORDER BY lane_id")] if "lanes" in tables else []
    source_ids = {c["effective_guide_id"] for c in persistent}
    remap = {}
    for channel in persistent:
        guide = channel["effective_guide_id"]
        if guide.startswith("lane."):
            if guide not in remap:
                alternate = "xtream.guide." + hashlib.sha256(guide.encode()).hexdigest()[:24]
                while alternate in source_ids:
                    alternate += ".guide"
                remap[guide] = alternate
            channel["effective_guide_id"] = remap[guide]
    # Persistent numbers never change. Persist a stable alternate for a lane
    # only when its original number collides, including on upgraded databases.
    reserved = {Decimal(c["channel_number"]) for c in list_channels(conn)}
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        saved = dict(conn.execute("SELECT lane_id,channel_number FROM channels_lane_numbers"))
        original = {Decimal(str(l.get("logical_number") or l["lane_id"])) for l in lanes}
        active_ids = {lane["lane_id"] for lane in lanes}
        allocated = reserved | {Decimal(number) for lane_id, number in saved.items() if lane_id not in active_ids}
        next_number = max([Decimal(str(get_setting(conn, "lane_start_ch", 9000))), *original, *reserved, *(Decimal(v) for v in saved.values())]) + 1000
        pending = []
        # Give existing saved assignments priority over newly added lanes.
        for lane in lanes:
            current = saved.get(lane["lane_id"])
            if current and Decimal(current) not in allocated:
                lane["export_number"] = current
                allocated.add(Decimal(current))
            else:
                pending.append(lane)
        for lane in pending:
            number = Decimal(str(lane.get("logical_number") or lane["lane_id"]))
            if number in allocated:
                while next_number in allocated or next_number in original:
                    next_number += 1
                number = next_number
                next_number += 1
            lane["export_number"] = format(number, "f")
            allocated.add(number)
            conn.execute("INSERT INTO channels_lane_numbers VALUES(?,?) ON CONFLICT(lane_id) DO UPDATE SET channel_number=excluded.channel_number",
                         (lane["lane_id"], lane["export_number"]))
    return persistent, lanes


def m3u(conn, server_url):
    persistent, lanes = lineup(conn)
    result = render_m3u(conn, server_url, channels=persistent).rstrip()
    from server.services.xtream_persistent import _attribute
    for lane in lanes:
        number = _attribute(lane["export_number"])
        lane_id = lane["lane_id"]
        result += (f'\n#EXTINF:-1 channel-id="lane.{lane_id}" tvg-id="lane.{lane_id}" tvg-name="Fruit Lane {lane_id}" '
                   f'channel-number="{number}" tvg-chno="{number}" group-title="FruitDeepLinks",Fruit Lane {lane_id}\n'
                   f'{server_url.rstrip("/")}/lane/{lane_id}/stream.m3u8')
    return result + "\n"


def xmltv(conn):
    accounts = load_accounts(conn)
    persistent, lanes = lineup(conn)
    # Cached programmes retain original guide IDs. Remap after loading, using
    # the persistent row ID so a namespace collision cannot attach wrong EPG.
    originals = {c["id"]: c for c in list_channels(conn, enabled_only=True)}
    tree = xmltv_tree(conn, channels=list(originals.values()))
    remap = {originals[c["id"]]["effective_guide_id"]: c["effective_guide_id"] for c in persistent}
    for element in tree:
        key = "id" if element.tag == "channel" else "channel"
        if element.get(key) in remap:
            element.set(key, remap[element.get(key)])
    if lanes:
        lane_tree = lanes_xmltv_tree(conn)
        numbers = {f'lane.{l["lane_id"]}': l["export_number"] for l in lanes}
        for element in lane_tree:
            if element.tag == "channel":
                names = element.findall("display-name")
                if len(names) > 1:
                    names[1].text = numbers[element.get("id")]
            tree.append(element)
    # XMLTV DTD requires all channels before programmes.
    tree[:] = [e for e in tree if e.tag == "channel"] + [e for e in tree if e.tag == "programme"]
    for element in tree.iter():
        if element.text:
            element.text = safe_value(element.text, accounts)
        if element.tail:
            element.tail = safe_value(element.tail, accounts)
        element.attrib.update(safe_value(element.attrib, accounts))
    ET.indent(tree, space="  ")
    return ET.tostring(tree, encoding="utf-8", xml_declaration=True)
