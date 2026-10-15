#ifndef BLDC_G431_PROTECTION_PROBE_H
#define BLDC_G431_PROTECTION_PROBE_H

#include <stdbool.h>
#include <stdint.h>

/* Target-private commissioning hook; never used by the normal protection owner. */
typedef enum {
    PROTECTION_PROBE_NONE = 0,
    PROTECTION_PROBE_OVP = 1,
    PROTECTION_PROBE_OCP_C = 2,
    PROTECTION_PROBE_OCP_AB = 3
} ProtectionProbeSource;

typedef enum {
    PROTECTION_PROBE_PREPARED = 1,
    PROTECTION_PROBE_OBSERVED = 2,
    PROTECTION_PROBE_REJECTED = 3,
    PROTECTION_PROBE_NOT_OBSERVED = 4
} ProtectionProbeOutcome;

/* ADC-owned seqlock, read before/after sequence: same even value required.
 * Evidence is sampled BEFORE software safe-off; elapsed_cycles is a polling
 * observation interval, not comparator-to-Gate propagation delay.
 * comp masks: COMP1=bit0 (C), COMP2=bit1 (B), COMP3=bit2 (VBUS), COMP4=bit3 (A).
 * Clear injection changes held only; the original evidence remains available. */
typedef struct {
    uint32_t sequence;
    uint32_t request_sequence;
    uint32_t source;
    uint32_t outcome;
    uint32_t held;
    uint32_t original_code;
    uint32_t expected_comp_mask;
    uint32_t expected_break_flag;
    uint32_t bdtr_before;
    uint32_t sr_before;
    uint32_t comp_before;
    uint32_t bdtr_after;
    uint32_t sr_after;
    uint32_t comp_after;
    uint32_t start_cycles;
    uint32_t elapsed_cycles;
    uint32_t poll_count;
} ProtectionProbeCapture;

extern volatile ProtectionProbeCapture g_target_validation_probe;

/* Prepare requires MOE=0; does not change any hardware. */
bool protection_probe_prepare(ProtectionProbeSource source, uint32_t request_sequence);
bool protection_probe_is_held(void);
/* Caller masks interrupts and MUST safe-off before restoring them, on all outcomes.
 * A single DAC threshold is lowered to zero; hardware Break remains enabled.
 * The injection stays active until an explicit idle clear, including on failure. */
ProtectionProbeOutcome protection_probe_fire(void);
/* Requires MOE=0. Restore the saved threshold only; never rearm or enable. */
bool protection_probe_clear(void);

#endif
