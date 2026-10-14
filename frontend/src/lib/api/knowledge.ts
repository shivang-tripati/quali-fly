import { useMutation, useQueryClient, queryOptions } from "@tanstack/react-query";
import { z } from "zod";

import { apiRequest } from "@/lib/api/client";

/**
 * The PRD's source vocabulary, as the API spells it.
 *
 * Declared as a union rather than `z.string()` so an unexpected value fails loudly instead
 * of rendering as a raw string in the list. The stored values are lowercase
 * (`KnowledgeSourceType.FAQ` is the *enum member*; `"faq"` is the wire value), which is
 * exactly the sort of mismatch a `string` type hides and a union does not.
 *
 * Declared before the schemas that use it: `z.object({...})` evaluates its fields at module
 * load, so referencing this from below would be a temporal-dead-zone error at import time
 * that type-checking would not catch.
 */
const sourceTypeSchema = z.enum(["faq", "document", "website", "manual"]);

/**
 * The knowledge base: "what does my AI know?"
 *
 * Phrased that way on purpose. The list is a set of documents a person wrote or uploaded,
 * not rows - so the schema carries `title`, `source_type`, `chunk_count` and `updated_at`,
 * which is everything needed to answer "is my pricing in there?" without opening anything.
 *
 * `content` appears only on the single-document read. A list response carrying every
 * document's full text would be a payload the operator never asked for.
 */
const summarySchema = z.object({
  document_id: z.string().uuid(),
  title: z.string(),
  source_type: sourceTypeSchema,
  is_active: z.boolean(),
  chunk_count: z.number(),
  updated_at: z.string().nullable(),
});

const documentSchema = summarySchema.extend({
  content: z.string(),
  metadata: z.record(z.string(), z.unknown()),
  created_at: z.string().nullable(),
});

const listSchema = z.object({ items: z.array(summarySchema), count: z.number() });

export type KnowledgeSourceType = z.infer<typeof sourceTypeSchema>;

export type KnowledgeSummary = z.infer<typeof summarySchema>;
export type KnowledgeDocument = z.infer<typeof documentSchema>;

/**
 * Human labels for the PRD's source vocabulary.
 *
 * Keyed on the *stored* values, which are lowercase - `KnowledgeSourceType.FAQ` is the enum
 * member, `"faq"` is what arrives over the wire. Keying the map on the member name instead
 * would silently fall through to the raw value and render "faq" in the list.
 */
export const sourceTypeLabels: Record<string, string> = {
  faq: "FAQ",
  document: "Document",
  website: "Website",
  manual: "Manual",
};

export const knowledgeKeys = { all: ["knowledge"] as const };

export function knowledgeOptions() {
  return queryOptions({
    queryKey: knowledgeKeys.all,
    queryFn: () => apiRequest("/api/v1/knowledge-base", listSchema),
  });
}

export function knowledgeDocumentOptions(documentId: string) {
  return queryOptions({
    queryKey: [...knowledgeKeys.all, "document", documentId] as const,
    queryFn: () => apiRequest(`/api/v1/knowledge-base/${documentId}`, documentSchema),
  });
}

/**
 * Create or replace a document.
 *
 * The backend treats a `document_id` in the body as "replace this one", so there is one
 * mutation rather than two. That is not a shortcut for its own sake - the document is
 * re-chunked and re-embedded on write, and two separate code paths for that would be two
 * places to forget the re-index.
 */
export function useSaveKnowledge() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: {
      documentId?: string;
      title: string;
      content: string;
      source_type?: KnowledgeSourceType;
    }) =>
      apiRequest("/api/v1/knowledge-base", summarySchema, {
        method: "POST",
        body: JSON.stringify({
          title: input.title,
          content: input.content,
          source_type: input.source_type ?? "document",
          document_id: input.documentId ?? null,
        }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: knowledgeKeys.all });
      // Chunk counts and the dashboard's knowledge count both change on a write.
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}

export function useDeleteKnowledge() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (documentId: string) =>
      apiRequest(`/api/v1/knowledge-base/${documentId}`, z.null(), { method: "DELETE" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: knowledgeKeys.all });
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}
