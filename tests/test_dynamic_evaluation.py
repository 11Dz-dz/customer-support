import unittest
from evals.dynamic_cases import cases,validate
from evals.dynamic_runner import (summarize,validate_simulator_decision,display_checks,
                                 displayed_product_ids,style_key)
from copy import deepcopy


class DynamicEvaluationTests(unittest.TestCase):
    def test_cards_unique_and_bounded(self):
        rows=validate(cases())
        self.assertEqual(len(rows),20)
        self.assertTrue(all(r['max_turns']==4 for r in rows))

    def test_duplicate_cards_rejected(self):
        with self.assertRaises(ValueError):validate([cases()[0],cases()[0]])

    def test_display_contract_rejects_boolean_count(self):
        row=deepcopy(cases()[0]);row['expected']['display_count']=True
        with self.assertRaises(ValueError):validate([row])

    def test_display_contract_on_three_style_cases(self):
        for row in cases():
            if row['id'] in {'D01','D02','D04'}:
                self.assertEqual(row['expected']['display_count'],3)
                self.assertEqual(row['expected']['distinct_policy'],'actual-spec-v1')

    def test_simulator_cannot_skip_followup(self):
        with self.assertRaises(ValueError):validate_simulator_decision({'done':True,'message':''},True)

    def test_simulator_non_boolean_rejected(self):
        with self.assertRaises(ValueError):validate_simulator_decision({'done':'false','message':'500以内'},False)

    def test_simulator_empty_message_rejected(self):
        with self.assertRaises(ValueError):validate_simulator_decision({'done':False,'message':' '},False)

    def test_simulator_valid_followup(self):
        self.assertFalse(validate_simulator_decision({'done':False,'message':'预算改成500以内'},True)['done'])

    def test_unavailable_not_counted_as_business_failure(self):
        r={'id':'D01','repeat':1,'passed':None,'status':'simulator_error','turns':[],'simulator':[]}
        s=summarize({'results':[r],'manifest':{'selected_ids':['D01'],'repeat':5}})
        self.assertEqual(s['scored_episodes'],0)
        self.assertIsNone(s['success_rate'])
        self.assertEqual(s['all_repeats_passed_fraction'],0)

    def test_reliability_requires_every_repeat(self):
        rows=[{'id':'D01','repeat':i,'passed':True,'status':'completed','turns':[],'simulator':[]} for i in range(1,5)]
        manifest={'selected_ids':['D01'],'repeat':5}
        self.assertEqual(summarize({'results':rows,'manifest':manifest})['all_repeats_passed_fraction'],0)
        rows.append({**rows[0],'repeat':5,'passed':False})
        self.assertEqual(summarize({'results':rows,'manifest':manifest})['all_repeats_passed_fraction'],0)
        rows[-1]['passed']=True
        self.assertEqual(summarize({'results':rows,'manifest':manifest})['all_repeats_passed_fraction'],1)


class DisplayContractTests(unittest.TestCase):
    def setUp(self):
        self.expected={'display_count':3,'distinct_policy':'actual-spec-v1','max_price':100}
        self.products=[self.product(707,'Red'),self.product(708,'Black'),self.product(711,'Blue')]

    def product(self,pid,color='Red',**kw):
        return dict(product_id=pid,name='Sport-100 Helmet',color=color,size=None,
                    product_subcategory_id=31,list_price='34.9900',**kw)

    def grade(self,ids,products=None,text='',shown=None):
        products=self.products if products is None else products
        answer='\n'.join(f'商品{pid}：头盔。' for pid in ids)+'\n'+text
        return {c['name']:c['passed'] for c in display_checks(self.expected,answer,
            {'scope_products':products,'shown_products':products if shown is None else shown})}

    def test_three_colors_of_same_model_pass(self):
        for row in self.products:row['product_model_id']=35
        self.assertTrue(all(self.grade([707,708,711]).values()))

    def test_same_style_repeated_three_times_fails(self):
        score=self.grade([707,707,707])
        self.assertFalse(score['displayed_styles_not_repeated'])
        self.assertFalse(score['displayed_style_count'])

    def test_different_ids_or_sku_numbers_do_not_inflate_count(self):
        rows=[{**self.products[0],'product_id':pid,'product_number':str(pid)} for pid in [1,2,3]]
        score=self.grade([1,2,3],rows)
        self.assertFalse(score['displayed_styles_not_repeated'])

    def test_sizes_are_distinct_styles(self):
        rows=[{**self.products[0],'product_id':i,'size':s} for i,s in [(1,'S'),(2,'M'),(3,'L')]]
        self.assertTrue(all(self.grade([1,2,3],rows).values()))

    def test_name_or_description_difference_counts(self):
        a=self.products[0];b={**a,'description_en':'A different construction'}
        self.assertNotEqual(style_key(a),style_key(b))
        self.assertNotEqual(style_key(a),style_key({**a,'name':'Other helmet'}))

    def test_identifier_or_price_change_alone_not_new_style(self):
        a=self.products[0]
        self.assertEqual(style_key(a),style_key({**a,'product_id':999,'product_number':'OTHER','list_price':'50'}))

    def test_three_retrieved_but_only_one_shown_fails(self):
        self.assertFalse(self.grade([707])['displayed_style_count'])

    def test_claiming_three_without_cards_fails(self):
        self.assertFalse(self.grade([],text='已检索到三款头盔。')['displayed_style_count'])

    def test_true_shortage_requires_explanation(self):
        rows=self.products[:2]
        self.assertFalse(self.grade([707,708],rows)['catalog_shortage_explained'])
        self.assertTrue(all(self.grade([707,708],rows,text='当前条件下只有两款头盔。').values()))

    def test_false_shortage_cannot_hide_available_products(self):
        self.assertFalse(self.grade([707],text='当前只有一款。')['displayed_style_count'])

    def test_above_budget_product_fails(self):
        rows=deepcopy(self.products);rows[2]['list_price']='101'
        self.assertFalse(self.grade([707,708,711],rows)['displayed_products_in_scope'])

    def test_wrong_category_fails(self):
        row={**self.products[0],'product_id':999,'product_subcategory_id':1}
        self.assertFalse(self.grade([707,708,999],shown=self.products+[row])['displayed_products_in_scope'])

    def test_unknown_product_fails(self):
        self.assertFalse(self.grade([707,708,999])['displayed_products_known'])

    def test_empty_scope_requires_explicit_no_match(self):
        self.assertFalse(self.grade([],[])['catalog_shortage_explained'])
        self.assertTrue(all(self.grade([],[],text='没有符合当前条件的头盔。').values()))

    def test_server_catalog_list_layout(self):
        answer='\n'.join(f'- Helmet（商品{p["product_id"]}，SKU），目录标价：34.99。' for p in self.products)
        self.assertEqual(displayed_product_ids(answer),[707,708,711])
        self.assertTrue(all(c['passed'] for c in display_checks(self.expected,answer,
            {'scope_products':self.products,'shown_products':self.products})))

    def test_mentions_and_order_lines_are_not_product_cards(self):
        self.assertEqual(displayed_product_ids('刚才的商品707价格没变。\n- Helmet（商品708）：34.99 × 2 = 69.98'),[])


if __name__=='__main__':unittest.main()
