/**
 * Re-export of TestRunnerPanel from the services page components.
 * * The canonical implementation lives in app/services/_components/TestRunnerPanel.tsx
 * where it is used by the services catalog page. This re-export allows other
 * parts of the application to import from the top-level components directory.
 */
export { default, type TestRunnerPanelProps } from "@/app/services/_components/TestRunnerPanel";
