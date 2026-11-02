"""Command-line gbXML/RDF conversion."""
from . import convert_rdf_to_gbxml, convert_gbxml_to_rdf

import argparse

parser = argparse.ArgumentParser(description="Convert BOT/MOOSAS RDF and gbXML files.")
sub = parser.add_subparsers(dest="command", required=True)

p1 = sub.add_parser("rdf-to-gbxml", help="Convert RDF Turtle to gbXML")
p1.add_argument("input")
p1.add_argument("output")
p1.add_argument("--rdf-format", default="turtle")

p2 = sub.add_parser("gbxml-to-rdf", help="Convert gbXML to RDF Turtle")
p2.add_argument("input")
p2.add_argument("output")
p2.add_argument("--rdf-format", default="turtle")
p2.add_argument("--base-uri", default="")

args = parser.parse_args()
if args.command == "rdf-to-gbxml":
    convert_rdf_to_gbxml(args.input, args.output, rdf_format=args.rdf_format)
elif args.command == "gbxml-to-rdf":
    convert_gbxml_to_rdf(args.input, args.output, rdf_format=args.rdf_format, base_uri=args.base_uri)
