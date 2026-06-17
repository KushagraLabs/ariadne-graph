# Dimension 2: FalkorDB Code-Graph Analysis

## Overview
FalkorDB Code-Graph is a FalkorDB-powered tool that indexes source code repositories into a knowledge graph with an interactive web UI, CLI, and GraphRAG chat capabilities.

## Architecture
- **Backend**: Python (Flask), connects to FalkorDB (Redis-compatible graph database using GraphBLAS)
- **Frontend**: React-based web UI with graph visualization (pan, zoom, search)
- **CLI**: `cgraph` tool for indexing, searching, exploring from terminal
- **Graph DB**: FalkorDB (in-memory, ultra-fast, Cypher-compatible, built on GraphBLAS sparse matrix operations)
- **Parsing**: Language-specific analyzers (Python, Java, C# supported)
- **LLM Integration**: LiteLLM for natural-language-to-Cypher translation

## Key Features
1. **Code Knowledge Graph**: Typed nodes (classes, functions, files) and relationships (calls, imports, inherits)
2. **Interactive Web UI**: React frontend with graph visualization
3. **GraphRAG Chat**: Natural language questions about codebase via LLM-powered Cypher generation
4. **CLI Tool (`cgraph`)**: JSON output for terminal-based exploration
5. **Git History Analysis**: Analyze how code evolves over time
6. **Multi-language**: Python, Java, C# (expandable)

## Graph Schema
- Nodes: Module, Class, Function (typed)
- Edges: CALLS, INHERITS_FROM, DEPENDS_ON
- Stored in FalkorDB with Cypher query interface

## API Endpoints
- Read: list_repos, graph_entities, get_neighbors, auto_complete, repo_info, find_paths, chat, list_commits
- Mutating: analyze_folder, analyze_repo, switch_commit

## Differentiators
- FalkorDB uses matrix-aware planner (converts Cypher to matrix algebra) vs pointer-based traversal
- Ultra-low latency graph traversal via GraphBLAS
- Natural language to Cypher via LLM
- Git commit-level analysis

## Limitations Relative to Code Hygiene MCP Plan
- Only 3 languages supported (Python, Java, C#)
- No MCP server integration (REST API only)
- No temporal modeling of code changes
- No incremental indexing mentioned
- No language adapter abstraction
- Tightly coupled to FalkorDB ecosystem
