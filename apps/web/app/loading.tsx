export default function Loading() {
  return (
    <div className="pageStack" aria-label="Loading research data">
      <div className="loadingHeader">
        <span />
        <strong />
      </div>
      <div className="metricGrid">
        {[0, 1, 2, 3].map((item) => (
          <div className="metricCard loadingCard" key={item}>
            <span />
            <strong />
            <small />
          </div>
        ))}
      </div>
      <div className="dashboardGrid">
        <div className="panel loadingPanel" />
        <div className="panel loadingPanel" />
      </div>
    </div>
  );
}
