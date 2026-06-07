/**
 * Forge function entrypoint for the AI Bot Task add-on.
 *
 * This is an intentionally minimal placeholder. The real implementation
 * will be added when the workflow integration is wired in.
 *
 * Two handlers are exported:
 *
 *   handler             - main entrypoint registered by `function.ai-bot-task-handler`.
 *                         Today it just echoes the Forge invocation context so the
 *                         add-on can be installed and smoke-tested end-to-end.
 *
 *   populateHedefRepo   - option-resolver for the `Hedef Repo` custom field.
 *                         The dropdown options are department-specific and must
 *                         be loaded at runtime from the platform's department
 *                         configuration. The placeholder returns an empty list
 *                         until the wire-up task lands.
 */

/* eslint-disable no-console */

/**
 * Default handler for AI Bot Task issue events.
 *
 * @param {object} event   - Forge event payload.
 * @param {object} context - Forge invocation context.
 * @returns {Promise<{ok: boolean, echoed: object}>}
 */
export const handler = async (event, context) => {
  console.log('[ai-bot-task] handler invoked', {
    eventType: event && event.eventType,
    accountId: context && context.accountId,
    cloudId: context && context.cloudId,
  });

  return {
    ok: true,
    echoed: {
      eventType: (event && event.eventType) || null,
      cloudId: (context && context.cloudId) || null,
    },
  };
};

/**
 * Resolver for the dynamic `Hedef Repo` dropdown options.
 *
 * The real implementation will fetch the department's repository slugs from
 * the automation-service (`GET /admin/departments/{id}` -> `bitbucket_repo_slugs`)
 * and return them as `[{ value, label }]`. Until that endpoint is wired in,
 * we return an empty list so the field renders without runtime errors.
 *
 * @returns {Promise<Array<{value: string, label: string}>>}
 */
export const populateHedefRepo = async () => {
  return [];
};

export default handler;
