/* The terms a builder said yes to. rutor keeps the version of TERMS.md its person accepted and
   sends it as X-Terms-Version with every request. A claim and an upload need it, and the event
   log keeps it beside the claim and the upload: that line is the receipt for the cell.

   The service cannot see the person say yes. It can see which version their rutor says they
   said yes to, refuse one that is older than today's, and remember it. A rutor from before
   this sends nothing and is refused with words that stand on their own, since that rutor
   prints only the message. A version newer than this one passes, so a CLI merged ahead of a
   deploy keeps working. Release, reads and the admin's routes ask for nothing. */

import { error } from './db.js';

/** The `Version` line of TERMS.md. A test in cli/tests holds the two together. */
export const TERMS_VERSION = '2026-11-25';

const SHAPE = /^\d{4}-\d{2}-\d{2}$/;

/**
 * The terms version the request says its person accepted, when it is today's or newer.
 * Anything else, a missing header or one of another shape among them, is null: only a date
 * ever reaches the event log.
 * @param {Request} request
 * @returns {string|null}
 */
export function acceptedTerms(request) {
  const said = (request.headers.get('X-Terms-Version') || '').trim();
  if (!SHAPE.test(said)) return null;
  return said >= TERMS_VERSION ? said : null;
}

/**
 * The answer to a claim or an upload whose person has not said yes to today's terms.
 * Nothing has changed: a lock the caller holds is kept.
 * @returns {Response}
 */
export function termsRefusal() {
  return error(
    428,
    'terms_needed',
    `Say yes to the terms of ${TERMS_VERSION} first. Update rutor (git pull, install it again), then run rutor terms. Any lock you hold is kept.`,
  );
}
