/** Top-level render guard. */
import React from "react";

/**
 * Catches render errors anywhere below it so a bug degrades to a readable
 * message instead of a white window.
 *
 * This matters more here than in a typical web app: JSE is an Electron desktop
 * app with no address bar and no reload button, so an uncaught render error
 * leaves the user with a blank frame and no way to recover or report it. The
 * fallback keeps the error text on screen and offers a reload.
 *
 * Must be a class: there is no hook equivalent of componentDidCatch.
 */
class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null, info: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    this.setState({ info });
    // Goes to the Electron devtools console and the main-process log.
    console.error("Unhandled render error:", error, info?.componentStack);
  }

  render() {
    const { error, info } = this.state;
    if (!error) return this.props.children;

    const detail = [String(error?.stack || error), info?.componentStack]
      .filter(Boolean)
      .join("\n\n");

    return (
      <main className="render-error">
        <h1>Something broke while drawing this screen</h1>
        <p>
          Your data is safe — this is a display fault, not a data fault. Reloading usually
          clears it. If it keeps happening, the details below identify where it failed.
        </p>
        <div className="render-error-actions">
          <button onClick={() => window.location.reload()}>Reload JSE</button>
          <button
            className="secondary"
            onClick={() => navigator.clipboard?.writeText(detail)}
          >
            Copy details
          </button>
        </div>
        <pre>{detail}</pre>
      </main>
    );
  }
}

export { ErrorBoundary };
