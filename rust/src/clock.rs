//! The one clock every timestamp comes from (backup ids, audit stamps, and the
//! plan and log stamps still to come).

use chrono::{DateTime, Utc};

/// Now, in UTC. Debug builds honour `ZELADOR_NOW` (RFC 3339) so the differential
/// test can freeze both implementations at one instant; release builds never do.
pub fn now() -> DateTime<Utc> {
    if cfg!(debug_assertions)
        && let Ok(frozen) = std::env::var("ZELADOR_NOW")
    {
        return DateTime::parse_from_rfc3339(&frozen)
            .expect("ZELADOR_NOW must be an RFC 3339 timestamp")
            .with_timezone(&Utc);
    }
    Utc::now()
}
