#pragma once

#include <string>

namespace tether::bluetooth {

    class BluezMonitor;

    // Without root the adapter's class of device cannot be set, but BlueZ derives its service bits from the
    // profiles registered on it: an HFP Hands-Free record sets the Audio bit. Registers one from this process
    // only when nobody else (PipeWire/WirePlumber, oFono) has, and never accepts a connection on it: the
    // record exists to be advertised, and a phone that connects anyway is refused cleanly.
    //
    // PipeWire's own Hands-Free (org.pipewire.Telephony on the session bus) is what carries call audio and control,
    // and it needs the phone's HFP connection. Our record rejects every connection, so it must never sit in
    // PipeWire's way: it is not registered while PipeWire telephony is up, and it is withdrawn when that appears.
    //
    // Returns true when a Hands-Free record is on the adapter after the call, whoever put it there.
    // `how` says which: "present" (already there), "pipewire" (PipeWire telephony owns it), "registered" (ours)
    // or the reason it failed.
    bool ensure_handsfree_record(BluezMonitor& monitor, std::string& how);

    // Takes our record away again, when we made one.
    void release_handsfree_record(BluezMonitor& monitor);

    // Re-examines who owns Hands-Free and registers or withdraws our record to match. Cheap to call often: it
    // does nothing more than once every few seconds. A no-op outside TETHER_NO_ROOT mode.
    void reconcile_handsfree_record(BluezMonitor& monitor);

    // What is on offer for the Hands-Free role right now.
    struct HandsfreeSources {
        bool ours_registered = false;    // our placeholder record is registered
        bool adapter_lists_hfp = false;  // the adapter's UUIDs include Hands-Free (our own record counts)
        bool pipewire_telephony = false; // org.pipewire.Telephony has an owner on the session bus
    };

    enum class HandsfreeStub {
        Register, // nobody provides Hands-Free: put our placeholder up
        Keep,     // ours is up and nobody else wants the role
        Withdraw, // ours is up and someone real has appeared: take it down
        Leave,    // someone else provides it and ours is not up: do nothing
    };

    // The decision itself, kept free of D-Bus so it can be tested. The adapter's UUID list includes our own
    // record once it is registered, so it only counts as another provider while ours is down.
    HandsfreeStub decide_handsfree_stub(const HandsfreeSources& sources);

} // namespace tether::bluetooth
