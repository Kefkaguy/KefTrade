export default function Loading() {
  return (
    <div className="pageStack" aria-busy="true">
      <div className="loadingHeader">
        <span />
        <strong />
      </div>
      <div className="metricGrid">
        {[0, 1, 2, 3].map((item) => (
          <article className="metricCard loadingCard" key={item}>
            <span />
            <strong />
            <small />
          </article>
        ))}
      </div>
      <div className="dashboardGrid">
        <div className="loadingPanel" />
        <div className="loadingPanel" />
      </div>
    </div>
  );
}
