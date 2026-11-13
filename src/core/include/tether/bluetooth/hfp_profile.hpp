#pragma once

#include <string>

namespace tether::bluetooth {

    class BluezMonitor;

    // Without root the adapter's class of device cannot be set, but BlueZ derives its service bits from the
    // profiles registered on it: an HFP Hands-Free record sets the Audio bit. Registers one from this process
    // only when nobody else (PipeWire/WirePlumber, oFono) has, and never accepts a connection on it: the
    // record exists to be advertised, and a phone that connects anyway is refused cleanly.
    //
    // Returns true when a Hands-Free record is on the adapter after the call, whoever put it there.
    // `how` says which: "present" (already there), "registered" (ours) or the reason it failed.
    bool ensure_handsfree_record(BluezMonitor& monitor, std::string& how);

    // Takes our record away again, when we made one.
    void release_handsfree_record(BluezMonitor& monitor);

} // namespace tether::bluetooth
