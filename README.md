# myallscripts

This repository contains the scripts, data, and automation utilities I use for my day-to-day tasks on my corporate laptop. It serves as a centralized collection of automation-related scripts and supporting resources designed to streamline and simplify routine activities.

It is meant to grow over time — as new day-to-day tasks come up, new scripts (or new script folders) get added here rather than living scattered across random locations. The current contents automate recurring work around Arista Qwrap Access Points and the WifiAgent traffic-generation service running on them: opening AP sessions, managing certificates, generating test traffic, and keeping the WifiAgent process healthy.

## Adding New Scripts

As new day-to-day automation needs come up, add them here rather than keeping one-off scripts elsewhere:

- Group related scripts under a purpose-named subfolder (e.g. `qwrap-traffic/`, `qwrap-certs/`) instead of dumping everything at the root.
- Give every script a short header/docstring describing what it does and how to run it.
- Update this README with a one-line entry (and a highlights section, if the script is non-trivial) whenever a new script or folder is added.
