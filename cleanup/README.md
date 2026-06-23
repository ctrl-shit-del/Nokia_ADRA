# ADRA Test Pipeline Cleanup

This directory contains scripts to reset your system state so you can repeatedly test the ADRA Installation Agent with full end-to-end integration software (NS-3 and ROS2).

## Uninstalling Test Software

When you're done testing an installation and want to reset the environment, run the cleanup script:

```bash
chmod +x uninstall.sh
./uninstall.sh
```

### What does this script do?

- **NS-3:** Recursively removes the cloned Git repository and all compiled build artifacts from `/tmp/ns-3-dev`. 
- **ROS2:** Uses `apt-get purge` to remove the `ros-humble-ros-base` packages and any unneeded dependencies, and removes the ROS2 repository lists and GPG keys from your `/etc/apt/` directory.

Once completed, you can go back to the ADRA Command Center web UI, select the profile again, and initiate a fresh test!
