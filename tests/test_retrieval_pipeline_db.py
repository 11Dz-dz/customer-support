"""Search ranking and exact-catalog contracts against the existing read-only catalog."""
from decimal import Decimal
import unittest

from postgres_config import connect_database
from adventureworks_service import AdventureWorksService
from retrieval_pipeline import lexical_expression


class RetrievalPipelineDatabaseTests(unittest.TestCase):
    def test_use_words_can_match_without_every_word_in_the_query(self):
        expression,args=lexical_expression('touring bike long distance')
        with connect_database() as c:
            rows=c.execute(f'SELECT product_id,{expression} AS score FROM product_catalog WHERE product_id=ANY(%s)',
                           [*args,[947,963]]).fetchall()
        scores={r['product_id']:r['score'] for r in rows}
        self.assertGreater(scores[947],0)
        self.assertGreater(scores[947],scores[963])

    def test_equivalent_use_phrase_enters_keyword_channel_with_hard_filters(self):
        result=AdventureWorksService().search_products('长途骑行',limit=20,max_price=100,product_category='components',
                                                     keyword_terms=['长途骑乘','long distance riding'])
        self.assertTrue(result['products'])
        self.assertTrue(all(0<Decimal(p['list_price'])<=100 and p['product_subcategory_id'] in range(4,18)
                            for p in result['products']))
        bar=next(p for p in result['products'] if p['product_id']==947)
        self.assertIn('keyword',bar['retrieval_sources'])

    def test_exact_catalog_counts_do_not_use_broader_recommendation_matching(self):
        with connect_database() as c:
            reference=c.execute("""SELECT count(*) AS n FROM product_catalog WHERE list_price>0 AND
                (name ILIKE %s OR product_number ILIKE %s OR coalesce(description_zh,'') ILIKE %s
                 OR coalesce(description_en,'') ILIKE %s OR
                 to_tsvector('english',coalesce(name,'')||' '||coalesce(description_en,'')) @@ plainto_tsquery('english',%s))""",
                ['%Mountain-100%']*4+['Mountain-100']).fetchone()['n']
        result=AdventureWorksService().query_products(keyword='Mountain-100',operation='count')
        self.assertEqual(result['matched_count'],reference)


if __name__=='__main__':unittest.main()
