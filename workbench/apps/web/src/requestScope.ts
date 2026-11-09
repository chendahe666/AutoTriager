export interface RequestTicket { scope: string; generation: number }

/** Fence secondary reads when a view changes, including away-and-back navigation. */
export class RequestScope {
  private scope: string | null = null;
  private generation = 0;

  enter(scope: string): RequestTicket {
    if (this.scope !== scope) { this.scope = scope; this.generation += 1; }
    return { scope, generation: this.generation };
  }

  capture(): RequestTicket | null {
    return this.scope === null ? null : { scope: this.scope, generation: this.generation };
  }

  isCurrent(ticket: RequestTicket | null): boolean {
    return Boolean(ticket && ticket.scope === this.scope && ticket.generation === this.generation);
  }

  invalidate(ticket: RequestTicket): void {
    if (this.isCurrent(ticket)) { this.scope = null; this.generation += 1; }
  }
}
