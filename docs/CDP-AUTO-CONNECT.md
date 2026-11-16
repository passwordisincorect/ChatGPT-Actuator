# Current Edge auto connection

`dom_browser_attach(cdp_url="auto")` uses an owner-specific local grant at
`security.browser_auto_connect` in the runtime config. Distribution defaults remain
disabled. The grant contains `enabled: true`, the authenticated `owner`, the verified
personal Edge `user_data_dir`, `profile_directory: "Default"`, and
`allow_temporary_focus: true`. Legacy Worker consent flags do not grant access to a
personal profile.

The existing `browser_dom` permissions must also enable the requested task:
`enabled`, `visible_browser`, and `allow_navigation` for attachment, `allow_read`
for reading, and an `allowed_hosts` entry for the selected site. The grant does
not override these permissions or grant browser mutations by itself.

Auto mode verifies the running Edge identity and its loopback listener, discovers
`DevToolsActivePort`, and handles only a newly correlated native CDP consent dialog.
When debugging is off, the bounded UIA helper opens its temporary settings window,
enables the exact remote-debugging checkbox, closes that window, and restores focus.
This needs the temporary-focus grant; it uses no physical keyboard or mouse input.
Avoid interacting with Edge during that brief setup. Managed policies are respected;
the helper never restarts Edge or launches a replacement profile.

Omit selectors to receive bounded tab metadata. Then pass one unique `tab_id` or
`tab_url`; only the selected page becomes readable. The connection is reused within
the authenticated Work Session. Unselected transports expire after 60 seconds.
After a disconnect, attachment verifies the current endpoint again and invalidates
old references; it never replays a click or mutation. Detaching keeps original tabs
open and closes only owned child tabs. Explicit endpoints retain manual consent.

For the separately activated local r2 patch, the CDP changes are staged in an
immutable slot. Its manifest must
show only CDP module changes and the surgical attach handler change; EDA modules
remain byte-identical. Activation holds the existing exclusive update lease,
restarts only Actuator's selected tunnel/backend, verifies health and inventory,
and restores the previous runtime on failure. Keep the original r2 slot for rollback.
The RC2 ZIP and wheel are separate installation packages; publishing them does not
select a new runtime slot or copy a private browser grant into an installation.
