"use client";

/**
 * EventHistoryTimeline - renders Temporal event history in chronological order.
 * Each event shows event_type, timestamp and a short summary.
 */

interface TemporalEvent {
  event_id?: number | string;
  event_type?: string;
  timestamp?: string;
  summary?: string;
  [key: string]: unknown;
}

interface EventHistoryTimelineProps {
  events: unknown[];
}

export default function EventHistoryTimeline({ events }: EventHistoryTimelineProps): JSX.Element {
  const typedEvents = events as TemporalEvent[];

  return (
    <section style={{ margin: "1.5rem 0" }}>
      <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "0.5rem" }}>
        Event History Timeline
      </h2>
      {typedEvents.length === 0 ? (
        <p style={{ color: "#9ca3af", fontSize: "0.875rem" }}>(no events)</p>
      ) : (
        <ol style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {typedEvents.map((evt, idx) => (
            <li
              key={evt.event_id ?? idx}
              style={{
                display: "flex",
                gap: "0.75rem",
                padding: "0.5rem 0",
                borderBottom: "1px solid #f3f4f6",
                fontSize: "0.875rem",
              }}
            >
              <span style={{ color: "#9ca3af", minWidth: "2rem", textAlign: "right" }}>
                {evt.event_id ?? idx + 1}
              </span>
              <span style={{ fontWeight: 500, minWidth: "12rem" }}>
                {evt.event_type ?? "UNKNOWN"}
              </span>
              <span style={{ color: "#6b7280", minWidth: "12rem" }}>
                {evt.timestamp ? new Date(evt.timestamp).toLocaleString() : "-"}
              </span>
              <span style={{ color: "#374151" }}>{evt.summary ?? ""}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
