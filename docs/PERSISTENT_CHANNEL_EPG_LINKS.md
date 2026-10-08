# Choosing an alternative EPG link

In **Persistent Channels**, open **Add Channel** or **Edit**. The setup form includes **Alternative EPG link**, a guide-name search field, and **Find similar EPG links**.

1. Search using the station or network name. The channel's original provider name is filled in initially.
2. Review similar channel names, their guide IDs, and the programme counts from the saved XMLTV feed. Links known only from the channel cache explicitly say their programmes have not been checked.
3. Select **Choose this guide** for a channel carrying the same schedule.
4. Select **Save Channel** to apply it. Clearing the alternative link restores the channel's own provider guide.

Name matching offers suggestions; Fruit does not choose a guide automatically. Regional, alternate, and time-shifted feeds can have different schedules despite similar names.

Browsing uses the saved channel cache and XMLTV link index without provider requests. Refresh Channel Cache or Refresh EPG Link Index when newer links are needed. Saving an enabled channel still requests the normal persistent-guide refresh.

The alternative is stored as `epg_source_id`. It overrides the provider XMLTV source while preserving the original channel identity, stream, and exported guide ID. Changing it clears cached programmes from the previous source. If the alternative is unavailable, Fruit reports that and retains only unexpired programmes previously obtained from that same selection; it does not silently switch to the original stream's schedule.

Verification: 57 persistent-channel and EPG/lineup tests passed, including offline link search, deduplication across the two saved caches, replacement of a native source, stable exported IDs, clearing old schedules, and restoring the native source. Safari preview verified candidate display and draft selection inside Edit Channel.
