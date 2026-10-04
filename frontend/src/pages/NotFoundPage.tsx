import { Link } from "react-router-dom";

import { Button, EmptyState, Panel } from "../components/ui/primitives";

export function NotFoundPage() {
  return (
    <Panel>
      <EmptyState title="This page does not exist" action={<Link to="/"><Button>Go to the dashboard</Button></Link>} />
    </Panel>
  );
}
