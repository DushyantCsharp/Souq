import type { FastifyInstance, FastifyReply, FastifyRequest } from "fastify";
import { createSchema, createYoga } from "graphql-yoga";

import { typeDefs } from "../generated/schema.js";
import type { GraphQLContext } from "./context.js";
import { resolvers } from "./resolvers.js";

type ServerContext = { req: FastifyRequest; reply: FastifyReply };

export interface GraphQLDeps extends Omit<GraphQLContext, "requestId" | "signal" | "log"> {
  graphiql: boolean;
}

export function registerGraphQL(app: FastifyInstance, deps: GraphQLDeps): void {
  const { graphiql, ...shared } = deps;
  const yoga = createYoga<ServerContext, GraphQLContext>({
    schema: createSchema<ServerContext & GraphQLContext>({ typeDefs, resolvers }),
    graphqlEndpoint: "/graphql",
    graphiql,
    landingPage: false,
    logging: {
      debug: (...args: unknown[]) => app.log.debug(args),
      info: (...args: unknown[]) => app.log.info(args),
      warn: (...args: unknown[]) => app.log.warn(args),
      error: (...args: unknown[]) => app.log.error(args),
    },
    context: ({ req }) => ({ ...shared, requestId: req.id, log: req.log }),
  });

  app.route({
    url: yoga.graphqlEndpoint,
    method: ["GET", "POST", "OPTIONS"],
    handler: async (req, reply) => {
      const response = await yoga.handleNodeRequestAndResponse(req, reply, { req, reply });
      response.headers.forEach((value, key) => {
        reply.header(key, value);
      });
      reply.status(response.status);
      reply.send(response.body);
      return reply;
    },
  });
}
