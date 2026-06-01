"use client";

/**
 * Operations page — operations overview & runner queue.
 */

import RunnerQueueCard from "./_components/RunnerQueueCard";

export default function OperationsPage(): JSX.Element {
  return (
    <div className="stack stack--lg">
      <header className="page-header">
        <div className="page-header__title-row">
          <div>
            <h1>Operasyonlar</h1>
            <p className="page-header__lede">
              SSH runner kuyruğu, workspace durumu ve operasyonel metriklerin
              gerçek zamanlı görünümü.
            </p>
          </div>
        </div>
      </header>

      <RunnerQueueCard />
    </div>
  );
}
