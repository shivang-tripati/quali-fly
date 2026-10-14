import { queryOptions, useMutation, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";

import { apiRequest } from "@/lib/api/client";

/**
 * Meta connections and leads.
 *
 * Every response is validated with Zod, so a credential accidentally added to a payload
 * is a type error rather than something quietly stored in React state. In particular
 * there is no field here for an access token: the API only ever reports whether one is
 * configured.
 */
const connectionSchema = z.object({
  id: z.string().uuid(),
  provider: z.literal("META"),
  status: z.enum(["ACTIVE", "INACTIVE"]),
  display_name: z.string().nullable(),
  page_id: z.string().nullable(),
  instagram_business_id: z.string().nullable(),
  whatsapp_business_account_id: z.string().nullable(),
  app_id: z.string().nullable(),
  webhook_verify_token_configured: z.boolean(),
  access_token_configured: z.boolean(),
  scopes: z.array(z.string()),
  token_expires_at: z.string().nullable(),
  connected_at: z.string().nullable(),
  last_webhook_at: z.string().nullable(),
  created_at: z.string(),
  updated_at: z.string(),
});

const oauthStartSchema = z.object({
  authorize_url: z.string(),
  expires_in: z.number(),
});

const leadSchema = z.object({
  id: z.string().uuid(),
  tenant_id: z.string().uuid(),
  meta_leadgen_id: z.string(),
  source: z.literal("META_LEAD_ADS"),
  status: z.enum(["NEW", "REPLIED", "QUALIFIED", "CLOSED", "GHOSTED", "HUMAN_HANDOFF"]),
  name: z.string().nullable(),
  email: z.string().nullable(),
  phone: z.string().nullable(),
  form_id: z.string().nullable(),
  ad_id: z.string().nullable(),
  provider_created_at: z.string().nullable(),
  field_data: z.record(z.string(), z.unknown()),
  created_at: z.string(),
  updated_at: z.string(),
});

const leadListSchema = z.object({
  items: z.array(leadSchema),
  count: z.number(),
});

const conversationSchema = z.object({
  id: z.string().uuid(),
  channel: z.enum(["MESSENGER", "INSTAGRAM"]),
  meta_participant_id: z.string(),
  meta_source_id: z.string().nullable(),
  meta_referral_ad_id: z.string().nullable(),
  lead_id: z.string().uuid().nullable(),
  lead_name: z.string().nullable(),
  meta_connection_id: z.string().uuid().nullable(),
  last_message_at: z.string().nullable(),
  messaging_window_expires_at: z.string().nullable(),
  within_messaging_window: z.boolean(),
  created_at: z.string(),
  updated_at: z.string(),
});

const conversationListSchema = z.object({
  items: z.array(conversationSchema),
  count: z.number(),
});

const messageSchema = z.object({
  id: z.string().uuid(),
  conversation_id: z.string().uuid(),
  channel: z.enum(["MESSENGER", "INSTAGRAM"]),
  direction: z.enum(["INBOUND", "OUTBOUND"]),
  delivery: z.enum(["PENDING", "SENT", "FAILED"]),
  meta_message_id: z.string().nullable(),
  message_type: z.string(),
  text: z.string().nullable(),
  attachments: z.array(z.record(z.string(), z.unknown())),
  sent_at: z.string(),
  delivered_at: z.string().nullable(),
  failure_reason: z.string().nullable(),
  ai_model: z.string().nullable(),
  ai_prompt_tokens: z.number().nullable(),
  ai_completion_tokens: z.number().nullable(),
  ai_latency_ms: z.number().nullable(),
  created_at: z.string(),
});

const messageListSchema = z.object({
  items: z.array(messageSchema),
  count: z.number(),
});

const channelInfoSchema = z.object({
  channel: z.enum(["MESSENGER", "INSTAGRAM"]),
  payload_objects: z.array(z.string()),
  required_permission: z.string(),
});

export type MetaConnection = z.infer<typeof connectionSchema>;
export type Lead = z.infer<typeof leadSchema>;
export type Conversation = z.infer<typeof conversationSchema>;
export type Message = z.infer<typeof messageSchema>;
export type ChannelInfo = z.infer<typeof channelInfoSchema>;

/** PRD Sprint 3.4 lifecycle, spelled out for display. */
export const leadStatusLabels: Record<Lead["status"], string> = {
  NEW: "New",
  REPLIED: "Replied",
  QUALIFIED: "Qualified",
  CLOSED: "Closed",
  GHOSTED: "Ghosted",
  HUMAN_HANDOFF: "Handed off",
};

export const channelLabels: Record<Conversation["channel"], string> = {
  MESSENGER: "Messenger",
  INSTAGRAM: "Instagram",
};

/** Why a send did not reach the person, in words rather than an enum. */
export const sendFailureLabels: Record<string, string> = {
  messaging_window_closed: "Meta's 24-hour window has closed",
  credential_unavailable: "the Meta connection has no usable access token",
  meta_unavailable: "Meta was unavailable; not sent",
  rejected_by_meta: "Meta rejected the message",
  recipient_unavailable: "the recipient is no longer reachable",
  not_sendable: "the message could not be sent",
};

export const getMetaConnections = () => apiRequest("/api/v1/meta/connections", z.array(connectionSchema));

export const getLeads = () => apiRequest("/api/v1/leads", leadListSchema);

/** Ask the backend for a Meta dialog URL. It holds a single-use state value. */
export const startMetaOAuth = () => apiRequest("/api/v1/meta/oauth/start", oauthStartSchema);

export const metaConnectionsOptions = () =>
  queryOptions({ queryKey: ["meta", "connections"] as const, queryFn: getMetaConnections, retry: false });

export const leadsOptions = () =>
  queryOptions({ queryKey: ["leads"] as const, queryFn: getLeads, retry: false });

// -- conversations -----------------------------------------------------------

export const getConversations = () =>
  apiRequest("/api/v1/conversations", conversationListSchema);

export const getConversation = (conversationId: string) =>
  apiRequest(`/api/v1/conversations/${conversationId}`, conversationSchema);

export const getMessages = (conversationId: string) =>
  apiRequest(`/api/v1/conversations/${conversationId}/messages`, messageListSchema);

export const getChannels = () => apiRequest("/api/v1/conversations/channels", z.array(channelInfoSchema));

/**
 * The inbox list, optionally narrowed to one lead.
 *
 * `leadId` is in the query key, not filtered in the browser, so the lead workspace's
 * "their conversations" is one request for one lead's threads rather than the whole inbox
 * plus a `find`.
 */
export function conversationsOptions(leadId?: string) {
  const search = leadId ? `?lead_id=${encodeURIComponent(leadId)}` : "";
  return queryOptions({
    queryKey: ["conversations", "list", leadId ?? "all"] as const,
    queryFn: () => apiRequest(`/api/v1/conversations${search}`, conversationListSchema),
    retry: false,
  });
}

/**
 * One thread, by id.
 *
 * This exists for the deep link. Selecting from the list alone silently breaks: a link to a
 * thread older than the first page is not in the list, and the component would fall back to
 * the newest thread and show a person reading the wrong conversation while the URL says
 * something else. Fetching the thread the URL names is the only version that cannot lie.
 */
export const conversationOptions = (conversationId: string) =>
  queryOptions({
    queryKey: ["conversations", conversationId] as const,
    queryFn: () => getConversation(conversationId),
    retry: false,
    enabled: Boolean(conversationId),
  });

export const messagesOptions = (conversationId: string) =>
  queryOptions({
    queryKey: ["conversations", conversationId, "messages"] as const,
    queryFn: () => getMessages(conversationId),
    retry: false,
    enabled: Boolean(conversationId),
  });

/**
 * Send a reply. The API answers 201 whether or not Meta accepted it, so the returned
 * message's `delivery` and `failure_reason` are the outcome - not the status code.
 */
export function useSendMessage(conversationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (text: string) =>
      apiRequest(`/api/v1/conversations/${conversationId}/messages`, messageSchema, {
        method: "POST",
        body: JSON.stringify({ text }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
  });
}

const instagramAccountSchema = z.object({
  instagram_business_id: z.string(),
  username: z.string().nullable(),
  name: z.string().nullable(),
  label: z.string(),
});

/**
 * The Instagram Business Accounts this connection's Page can operate, discovered
 * server-side from the stored Meta credential. Nothing here is typed by the browser: the
 * select sends back one of these ids and the server re-checks it against Meta.
 */
const instagramAccountsSchema = z.object({
  page_id: z.string(),
  page_name: z.string().nullable(),
  connected_id: z.string().nullable(),
  accounts: z.array(instagramAccountSchema),
});

export function instagramAccountsOptions(connectionId: string) {
  return queryOptions({
    queryKey: ["meta", "connections", connectionId, "instagram-accounts"] as const,
    queryFn: () =>
      apiRequest(`/api/v1/meta/connections/${connectionId}/instagram-accounts`, instagramAccountsSchema),
    retry: false,
  });
}

/**
 * Attach an Instagram Business Account. The id is a proposal; the server confirms the
 * account is actually linked to this Page before storing it.
 */
export function useSelectInstagramAccount(connectionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (instagramBusinessId: string) =>
      apiRequest(`/api/v1/meta/connections/${connectionId}/instagram`, connectionSchema, {
        method: "POST",
        body: JSON.stringify({ instagram_business_id: instagramBusinessId }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["meta"] });
    },
  });
}

/**
 * This tenant's WhatsApp setup state.
 *
 * Four states, not a boolean - see the API. `configured` means the tenant finished setup,
 * `ready` additionally means this deployment could actually attempt a send. Nothing here
 * asserts that a message was ever delivered.
 */
const whatsappSetupSchema = z.object({
  state: z.enum([
    "not_connected",
    "awaiting_account",
    "awaiting_number",
    "configured",
    "ready",
  ]),
  ready: z.boolean(),
  whatsapp_business_account_id: z.string().nullable(),
  phone_number_id: z.string().nullable(),
  display_number: z.string().nullable(),
  platform_available: z.boolean(),
  unavailable_reason: z.string().nullable(),
  meta_connection_id: z.string().nullable(),
});

const whatsappNumberSchema = z.object({
  phone_number_id: z.string(),
  display_number: z.string().nullable(),
  verified_name: z.string().nullable(),
  is_verified: z.boolean(),
  label: z.string(),
});

export function whatsappSetupOptions() {
  return queryOptions({
    queryKey: ["tenant", "whatsapp"] as const,
    queryFn: () => apiRequest("/api/v1/tenant/whatsapp", whatsappSetupSchema),
    retry: false,
  });
}

export function useSelectWhatsAppBusinessAccount(connectionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (whatsappBusinessAccountId: string) =>
      apiRequest(
        `/api/v1/meta/connections/${connectionId}/whatsapp/business-account`,
        connectionSchema,
        {
          method: "POST",
          body: JSON.stringify({
            whatsapp_business_account_id: whatsappBusinessAccountId,
          }),
        },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["meta"] });
      void queryClient.invalidateQueries({ queryKey: ["tenant"] });
    },
  });
}

export function whatsappNumbersOptions(connectionId: string | null) {
  return queryOptions({
    queryKey: ["meta", "connections", connectionId, "whatsapp-numbers"] as const,
    queryFn: () =>
      apiRequest(
        `/api/v1/meta/connections/${connectionId}/whatsapp-numbers`,
        z.object({
          whatsapp_business_account_id: z.string().nullable(),
          connected_id: z.string().nullable(),
          numbers: z.array(whatsappNumberSchema),
        }),
      ),
    // No connection to ask about until one exists.
    enabled: Boolean(connectionId),
    retry: false,
  });
}

export function useSelectWhatsAppNumber(connectionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (phoneNumberId: string) =>
      apiRequest(`/api/v1/meta/connections/${connectionId}/whatsapp`, connectionSchema, {
        method: "POST",
        body: JSON.stringify({ phone_number_id: phoneNumberId }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["meta"] });
      void queryClient.invalidateQueries({ queryKey: ["tenant"] });
    },
  });
}

/** Disconnect. Server-side authorization decides whether this is allowed at all. */
export function useDisconnectMeta() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (connectionId: string) =>
      apiRequest(`/api/v1/meta/connections/${connectionId}`, connectionSchema, { method: "DELETE" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["meta"] });
    },
  });
}
